#pragma vengine_package vengine_directx
#include <DXRuntime/CommandAllocator.h>
#include <DXRuntime/CommandQueue.h>
namespace toolhub::directx {
namespace detail {

}// namespace detail
void CommandAllocator::CollectBuffer(CommandBuffer *buffer) {
    bufferPool.push_back(buffer);
}
void CommandAllocator::Execute(
    CommandQueue *queue,
    ID3D12Fence *fence,
    uint64 fenceIndex) {
    if (!executeCache.empty()) {
        queue->Queue()->ExecuteCommandLists(
            executeCache.size(),
            executeCache.data());
        executeCache.clear();
    }
    ThrowIfFailed(queue->Queue()->Signal(fence, fenceIndex));
}
void CommandAllocator::Complete(
    CommandQueue *queue,
    ID3D12Fence *fence,
    uint64 fenceIndex) {
    uint64 completeValue;
    if (fenceIndex > 0 && fence->GetCompletedValue() < fenceIndex) {
        ThrowIfFailed(fence->SetEventOnCompletion(fenceIndex, device->EventHandle()));
        WaitForSingleObject(device->EventHandle(), INFINITE);
    }
    while (auto evt = executeAfterComplete.Pop()) {
        (*evt)();
    }
}
vstd::unique_ptr<CommandBuffer> CommandAllocator::GetBuffer() {
    auto dev = [&] {
        if (bufferPool.empty())
            return bufferAllocator.New(device, this);
        else
            return bufferPool.erase_last();
    }();
    executeCache.push_back(dev->CmdList());
    return vstd::create_unique(dev);
}
CommandAllocator::CommandAllocator(
    Device *device,
    IGpuAllocator *resourceAllocator,
    D3D12_COMMAND_LIST_TYPE type)
    : bufferAllocator(32, false),
      type(type),
      resourceAllocator(resourceAllocator),
      device(device),
      uploadAllocator(TEMP_SIZE, &ubVisitor),
      readbackAllocator(TEMP_SIZE, &rbVisitor),
      defaultAllocator(TEMP_SIZE, &dbVisitor) {
    rbVisitor.self = this;
    ubVisitor.self = this;
    dbVisitor.self = this;
    ThrowIfFailed(
        device->device->CreateCommandAllocator(type, IID_PPV_ARGS(allocator.GetAddressOf())));
}
CommandAllocator::~CommandAllocator() {
    for (auto &&i : bufferPool) {
        i->~CommandBuffer();
    }
}
void CommandAllocator::Reset(CommandQueue *queue) {
    readbackAllocator.Clear();
    uploadAllocator.Clear();
    defaultAllocator.Clear();
    ThrowIfFailed(
        allocator->Reset());
}

DefaultBuffer const *CommandAllocator::AllocateScratchBuffer(size_t targetSize) {
    if (scratchBuffer) {
        if (scratchBuffer->GetByteSize() < targetSize) {
            size_t allocSize = scratchBuffer->GetByteSize();
            while (allocSize < targetSize) {
                allocSize = std::max<size_t>(allocSize + 1, allocSize * 1.5f);
            }
            executeAfterComplete.Push([s = std::move(scratchBuffer)]() {});
            allocSize = CalcAlign(allocSize, 65536);
            scratchBuffer = vstd::create_unique(new DefaultBuffer(device, allocSize, device->defaultAllocator, D3D12_RESOURCE_STATE_UNORDERED_ACCESS));
        }
        return scratchBuffer.get();
    } else {
        targetSize = CalcAlign(targetSize, 65536);
        scratchBuffer = vstd::create_unique(new DefaultBuffer(device, targetSize, device->defaultAllocator, D3D12_RESOURCE_STATE_UNORDERED_ACCESS));
        return scratchBuffer.get();
    }
}

template<typename Pack>
uint64 CommandAllocator::Visitor<Pack>::Allocate(uint64 size) {
    auto packPtr = new Pack(
        self->device,
        size,
        self->resourceAllocator);
    return reinterpret_cast<uint64>(packPtr);
}
template<typename Pack>
void CommandAllocator::Visitor<Pack>::DeAllocate(uint64 handle) {
    delete reinterpret_cast<Pack *>(handle);
}
vstd::StackAllocator::Chunk CommandAllocator::Allocate(
    vstd::StackAllocator &allocator,
    uint64 size,
    size_t align) {
    if (align <= 1) {
        return allocator.Allocate(size);
    }
    return allocator.Allocate(size, align);
}

BufferView CommandAllocator::GetTempReadbackBuffer(uint64 size, size_t align) {
    auto chunk = Allocate(readbackAllocator, size, align);
    auto package = reinterpret_cast<ReadbackBuffer *>(chunk.handle);
    return {
        package,
        chunk.offset,
        size};
}

BufferView CommandAllocator::GetTempUploadBuffer(uint64 size, size_t align) {
    auto chunk = Allocate(uploadAllocator, size, align);
    auto package = reinterpret_cast<UploadBuffer *>(chunk.handle);
    return {
        package,
        chunk.offset,
        size};
}
BufferView CommandAllocator::GetTempDefaultBuffer(uint64 size, size_t align) {
    auto chunk = Allocate(defaultAllocator, size, align);
    auto package = reinterpret_cast<DefaultBuffer *>(chunk.handle);
    return {
        package,
        chunk.offset,
        size};
}

}// namespace toolhub::directx