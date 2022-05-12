import ast
import astpretty
import inspect
import sys
from types import SimpleNamespace, ModuleType
from .types import length_of, element_of, vector
from . import globalvars
import lcapi
from .builtin import builtin_func_names, builtin_func, builtin_bin_op, builtin_type_cast, \
    builtin_unary_op, callable_call, wrap_with_tmp_var
from .types import dtype_of, to_lctype, CallableType, vector_dtypes, matrix_dtypes
from .types import BuiltinFuncType, BuiltinFuncBuilder
from .vector import is_swizzle_name, get_swizzle_code, get_swizzle_resulttype
from .array import ArrayType
from .struct import StructType

def ctx():
    return globalvars.current_context


class VariableInfo:
    def __init__(self, dtype, expr, is_arg = False):
        self.dtype = dtype
        self.expr = expr
        self.is_arg = is_arg

# Visit AST of the python functions; computes the following for each expression node:
# node.dtype: type of the expression
# node.expr: the expression as defined in the function builder
# node.lr: "l" or "r", representing l-value or r-value

class ASTVisitor:
    def __call__(self, node):
        method = getattr(self, 'build_' + node.__class__.__name__, None)
        if method is None:
            raise NotImplementedError(f'Unsupported syntax node {node}')
        try:
            # print(astpretty.pformat(node))
            self.comment_source(node)
            return method(node)
        except Exception as e:
            if not hasattr(e, "already_printed"):
                self.print_error(node, e)
                e.already_printed = True
            raise

    @staticmethod
    def comment_source(node):
        n = node.lineno-1
        if getattr(ctx(), "_last_comment_source_lineno", None) != n:
            ctx()._last_comment_source_lineno = n
            lcapi.builder().comment_(str(n) + "  " + ctx().sourcelines[n].strip())

    @staticmethod
    def print_error(node, e):
        # print exception, line number and corresponding source code
        def support_color():
            try:
                shell = get_ipython().__class__.__name__
                return shell in ('ZMQInteractiveShell', 'TerminalInteractiveShell')
            except NameError:
                return sys.stdout.isatty()
        if support_color():
            red, green, bold, clr = "\x1b[31;1m", "\x1b[32;1m", "\x1b[1m", "\x1b[0m"
        else:
            red = green = bold = clr = ""
        prefix = f"{bold}{ctx().func.filename.split('/')[-1]}:{ctx().func.lineno-2+node.lineno} {clr}"
        if type(e).__name__ == "CompileError":
            print(f"{prefix}{red}Error:{clr}{bold} The above error occured during compilation of '{e.func.__name__}'{clr}")
        else:
            print(f"{prefix}{red}Error:{clr}{bold} {type(e).__name__}: {e}{clr}")
        source = ctx().sourcelines[node.lineno-1: node.end_lineno]
        for idx,line in enumerate(source):
            print(line.rstrip('\n'))
            startcol = node.col_offset if idx==0 else 0
            endcol = node.end_col_offset if idx==len(source)-1 else len(line)
            print(green + ' ' * startcol + '~' * (endcol - startcol) + clr)
        print(f"in luisa.func '{ctx().func.__name__}' in {ctx().func.filename}")
        if type(e).__name__ != "CompileError":
            import traceback
            traceback.print_exc(limit=-2)
            # print("Traceback (most recent call last):")
            # _, _, tb = sys.exc_info()
            # traceback.print_tb(tb,limit=-1) # Fixed format
            print() # blank line

    @staticmethod
    def build_FunctionDef(node):
        for x in node.body:
            build(x)

    @staticmethod
    def build_Expr(node):
        if isinstance(node.value, ast.Call):
            build(node.value)
            if node.value.dtype != None:
                raise TypeError("Discarding non-void return value")
        else:
            if not isinstance(node.value, ast.Constant):
                raise TypeError("Dangling expression")

    @staticmethod
    def build_Return(node):
        if node.value != None:
            build(node.value)
        # deduce & check type of return value
        return_type = None if node.value == None else node.value.dtype
        if hasattr(ctx(), 'return_type'):
            if ctx().return_type != return_type:
                raise TypeError("inconsistent return type in multiple return statements")
        else:
            ctx().return_type = return_type
            if ctx().call_from_host and return_type != None:
                raise TypeError("luisa func called on host can't return value")
        # build return statement
        lcapi.builder().return_(getattr(node.value, 'expr', None))

    @staticmethod
    def build_Call(node):
        build(node.func)
        for x in node.args:
            build(x)
        for x in node.keywords:
            build(x.value)
        # if it's called as method, call with self (the object)
        if type(node.func) is ast.Attribute and getattr(node.func, 'calling_method', False):
            args = [node.func.value] + node.args 
        else:
            args = node.args
        kwargs = {x.arg: x.value for x in node.keywords}
        # custom function
        if node.func.dtype is CallableType:
            node.dtype, node.expr = callable_call(node.func.expr, *args, **kwargs)
        # builtin function (called by name string, as defined in builtin_func_names)
        elif node.func.dtype is BuiltinFuncType:
            node.dtype, node.expr = builtin_func(node.func.expr, *args, **kwargs)
        elif node.func.dtype is BuiltinFuncBuilder:
            node.dtype, node.expr = node.func.expr.builder(*args, **kwargs)
        # type: cast / construct
        elif node.func.dtype is type:
            dtype = node.func.expr
            node.dtype, node.expr = builtin_type_cast(dtype, *args, **kwargs)
        else:
            raise TypeError(f"{node.func.dtype} is not callble")
        node.lr = 'r'

    @staticmethod
    def build_Attribute(node):
        build(node.value)
        # vector swizzle
        if node.value.dtype in vector_dtypes:
            if is_swizzle_name(node.attr):
                original_size = to_lctype(node.value.dtype).dimension()
                swizzle_size = len(node.attr)
                swizzle_code = get_swizzle_code(node.attr, original_size)
                node.dtype = get_swizzle_resulttype(node.value.dtype, swizzle_size)
                node.expr = lcapi.builder().swizzle(to_lctype(node.dtype), node.value.expr, swizzle_size, swizzle_code)
                node.lr = 'l' if swizzle_size==1 else 'r'
            else:
                raise AttributeError(f"vector has no attribute '{node.attr}'")
        # struct member
        elif type(node.value.dtype) is StructType:
            if node.attr in node.value.dtype.idx_dict: # data member
                idx = node.value.dtype.idx_dict[node.attr]
                node.dtype = node.value.dtype.membertype[idx]
                node.expr = lcapi.builder().member(to_lctype(node.dtype), node.value.expr, idx)
                node.lr = node.value.lr
            elif node.attr in node.value.dtype.method_dict: # struct method
                node.dtype = CallableType
                node.calling_method = True
                node.expr = node.value.dtype.method_dict[node.attr]
            else:
                raise AttributeError(f"struct {node.value.dtype} has no attribute '{node.attr}'")
        elif node.value.dtype is ModuleType:
            node.dtype, node.expr, node.lr = build.captured_expr(getattr(node.value.expr, node.attr))
        elif hasattr(node.value.dtype, node.attr):
            entry = getattr(node.value.dtype, node.attr)
            if type(entry).__name__ == "func":
                node.dtype, node.expr = CallableType, entry
                node.calling_method = True
            elif type(entry) is BuiltinFuncBuilder:
                node.dtype, node.expr = BuiltinFuncBuilder, entry
                node.calling_method = True
            else:
                raise TypeError(f"Can't access attribute {node.attr} ({entry}) in luisa func")
        else:
            raise AttributeError(f"type {node.value.dtype} has no attribute '{node.attr}'")

    @staticmethod
    def build_Subscript(node):
        build(node.value)
        node.lr = node.value.lr
        build(node.slice)
        if type(node.value.dtype) is ArrayType:
            node.dtype = node.value.dtype.dtype
        elif node.value.dtype in vector_dtypes:
            node.dtype = element_of(node.value.dtype)
        elif node.value.dtype in matrix_dtypes:
            # matrix indexed is a column vector
            node.dtype = vector(float, length_of(node.value.dtype))
        else:
            raise TypeError(f"{node.value.dtype} object is not subscriptable")
        node.expr = lcapi.builder().access(to_lctype(node.dtype), node.value.expr, node.slice.expr)

    # external variable captured in kernel -> (dtype, expr, lr)
    @staticmethod
    def captured_expr(val):
        dtype = dtype_of(val)
        if dtype == ModuleType:
            return dtype, val, None
        if dtype == type:
            return dtype, val, None
        if dtype == CallableType:
            return dtype, val, None
        if dtype == BuiltinFuncBuilder:
            return dtype, val, None
        if dtype == str:
            return dtype, val, 'r'
        lctype = to_lctype(dtype)
        if lctype.is_basic():
            return dtype, lcapi.builder().literal(lctype, val), 'r'
        if lctype.is_buffer():
            return dtype, lcapi.builder().buffer_binding(lctype, val.handle, 0, val.bytesize), 'l' # offset defaults to 0
        if lctype.is_texture():
            return dtype, lcapi.builder().texture_binding(lctype, val.handle, 0), 'l' # miplevel defaults to 0
        if lctype.is_bindless_array():
            return dtype, lcapi.builder().bindless_array_binding(val.handle), 'l'
        if lctype.is_accel():
            return dtype, lcapi.builder().accel_binding(val.handle), 'l'
        if lctype.is_array():
            # create array and assign each element
            expr = lcapi.builder().local(lctype)
            for idx,x in enumerate(val.values):
                sliceexpr = lcapi.builder().literal(to_lctype(int), idx)
                lhs = lcapi.builder().access(lctype, expr, sliceexpr)
                rhs = lcapi.builder().literal(lctype.element(), x)
                lcapi.builder().assign(lhs, rhs)
            return dtype, expr, 'r'
        if lctype.is_structure():
            # create struct and assign each element
            expr = lcapi.builder().local(lctype)
            for idx,x in enumerate(val.values):
                lhs = lcapi.builder().member(to_lctype(dtype.membertype[idx]), expr, idx)
                rhs_dtype, rhs_expr, rhs_lr = build.captured_expr(x)
                assert rhs_dtype == dtype.membertype[idx]
                lcapi.builder().assign(lhs, rhs_expr)
            return dtype, expr, 'r'
        raise TypeError("unrecognized closure var type:", type(val))

    @staticmethod
    def build_Name(node, allow_none = False):
        # Note: in Python all local variables are function-scoped
        if node.id in builtin_func_names:
            node.dtype, node.expr = BuiltinFuncType, node.id
        elif node.id in ctx().local_variable:
            varinfo = ctx().local_variable[node.id]
            node.dtype = varinfo.dtype
            node.expr = varinfo.expr
            node.is_arg = varinfo.is_arg
            node.lr = 'l'
        else:
            val = ctx().closure_variable.get(node.id)
            if val is None:
                if not allow_none:
                    raise NameError(f"undeclared idenfitier '{node.id}'")
                node.dtype = None
                return
            node.dtype, node.expr, node.lr = build.captured_expr(val)

    @staticmethod
    def build_Constant(node):
        node.dtype = dtype_of(node.value)
        if node.dtype is str:
            node.expr = node.value
        else:
            node.expr = lcapi.builder().literal(to_lctype(node.dtype), node.value)
        node.lr = 'r'

    @staticmethod
    def build_assign_pair(lhs, rhs):
        if rhs.dtype == None:
            raise TypeError("Can't assign None to variable")
        # allows left hand side to be undefined
        if type(lhs) is ast.Name:
            build.build_Name(lhs, allow_none=True)
            if getattr(lhs, "is_arg", False): # is argument
                if lhs.dtype not in (int, float, bool): # not scalar; therefore passed by reference
                    raise TypeError("Assignment to non-scalar argument is not allowed. Please create a local variable.")
        else:
            build(lhs)
        # create local variable if it doesn't exist yet
        if lhs.dtype is None:
            dtype = rhs.dtype # craete variable with same type as rhs
            lhs.expr = lcapi.builder().local(to_lctype(dtype))
            # store type & ptr info into name
            ctx().local_variable[lhs.id] = VariableInfo(dtype, lhs.expr)
            # Note: all local variables are function scope
        else:
            # must assign with same type; no implicit casting is allowed.
            if lhs.dtype != rhs.dtype:
                raise TypeError(f"Can't assign to {lhs.dtype} with {rhs.dtype} ")
            if lhs.lr == "r":
                raise TypeError("Can't assign to read-only value")
        lcapi.builder().assign(lhs.expr, rhs.expr)

    @staticmethod
    def build_Assign(node):
        build(node.value)
        if len(node.targets) == 1:
            build.build_assign_pair(node.targets[0], node.value)
        else: # chained assignment
            wrap_with_tmp_var(node.value)
            for targ in node.targets:
                build.build_assign_pair(targ, node.value)

    @staticmethod
    def build_AugAssign(node):
        build(node.target)
        build(node.value)
        dtype, expr = builtin_bin_op(type(node.op), node.target, node.value)
        lcapi.builder().assign(node.target.expr, expr)

    @staticmethod
    def build_UnaryOp(node):
        build(node.operand)
        node.dtype, node.expr = builtin_unary_op(type(node.op), node.operand)
        node.lr = 'r'

    @staticmethod
    def build_BinOp(node):
        build(node.left)
        build(node.right)
        node.dtype, node.expr = builtin_bin_op(type(node.op), node.left, node.right)
        node.lr = 'r'

    @staticmethod
    def build_Compare(node):
        build(node.left)
        for x in node.comparators:
            build(x)
        node.dtype, node.expr = builtin_bin_op(type(node.ops[0]), node.left, node.comparators[0])
        # compare from left to right
        for idx in range(1, len(node.comparators)):
            obj = SimpleNamespace()
            obj.dtype, obj.expr = builtin_bin_op(type(node.ops[idx]), node.comparators[idx-1], node.comparators[idx])
            node.dtype, node.expr = builtin_bin_op(ast.And, node, obj)
        node.lr = 'r'

    @staticmethod
    def build_BoolOp(node):
        for x in node.values:
            build(x)
        node.dtype, node.expr = builtin_bin_op(type(node.op), node.values[0], node.values[1])
        # bool operators of same type are left-associative
        for idx in range(2, len(node.values)):
            node.dtype, node.expr = builtin_bin_op(type(node.op), node, node.values[idx])
        node.lr = 'r'

    @staticmethod
    def build_If(node):
        # condition
        build(node.test)
        if node.test.dtype != bool:
            raise TypeError(f"If condition must be bool, got {node.test.dtype}")
        ifstmt = lcapi.builder().if_(node.test.expr)
        # branches
        with ifstmt.true_branch():
            for x in node.body:
                build(x)
        with ifstmt.false_branch():
            for x in node.orelse:
                build(x)

    @staticmethod
    def build_IfExp(node):
        build(node.body)
        build(node.test)
        build(node.orelse)
        from lcapi import bool2, bool3, bool4
        if node.test.dtype not in {bool, bool2, bool3, bool4}:
            raise TypeError(f"IfExp condition must be bool or bool vector, got {node.test.dtype}")
        if node.body.dtype != node.orelse.dtype:
            raise TypeError(f"Both result expressions of IfExp must be of same type. ({node.body.dtype} vs {node.orelse.dtype})")
        if node.test.dtype != bool and length_of(node.test.dtype) != length_of(node.body.dtype):
            raise TypeError(f"IfExp condition must be either bool or vector of same length ({length_of(node.test.dtype)} != {length_of(node.body.dtype)})")
        node.dtype = node.body.dtype
        node.expr = lcapi.builder().call(to_lctype(node.dtype), lcapi.CallOp.SELECT, [node.orelse.expr, node.body.expr, node.test.expr])
        node.lr = 'r'

    @staticmethod
    def build_range_for(node):
        if len(node.iter.args) not in {1,2,3}:
            raise TypeError(f"'range' expects 1/2/3 arguments, got {en(node.iter.args)}")
        for x in node.iter.args:
            build(x)
            assert x.dtype is int
        if len(node.iter.args) == 1:
            range_start = lcapi.builder().literal(to_lctype(int), 0)
            range_stop = node.iter.args[0].expr
            range_step = lcapi.builder().literal(to_lctype(int), 1)
        if len(node.iter.args) == 2:
            range_start, range_stop = [x.expr for x in node.iter.args]
            range_step = lcapi.builder().literal(to_lctype(int), 1)
        if len(node.iter.args) == 3:
            range_start, range_stop, range_step = [x.expr for x in node.iter.args]
        # loop variable
        varexpr = lcapi.builder().local(to_lctype(int))
        lcapi.builder().assign(varexpr, range_start)
        ctx().local_variable[node.target.id] = VariableInfo(int, varexpr)
        # build for statement
        condition = lcapi.builder().binary(to_lctype(bool), lcapi.BinaryOp.LESS, varexpr, range_stop)
        forstmt = lcapi.builder().for_(varexpr, condition, range_step)
        with forstmt.body():
            for x in node.body:
                build(x)

    @staticmethod
    def build_container_for(node):
        range_start = lcapi.builder().literal(to_lctype(int), 0)
        range_stop = lcapi.builder().literal(to_lctype(int), length_of(node.iter.dtype))
        range_step = lcapi.builder().literal(to_lctype(int), 1)
        # loop variable
        idxexpr = lcapi.builder().local(to_lctype(int))
        lcapi.builder().assign(idxexpr, range_start)
        eltype = element_of(node.iter.dtype) if node.iter.dtype not in matrix_dtypes else vector(float, length_of(node.iter.dtype)) # iterating through matrix yields vectors
        varexpr = lcapi.builder().access(to_lctype(eltype), node.iter.expr, idxexpr) # loop variable (element)
        ctx().local_variable[node.target.id] = VariableInfo(eltype, varexpr)
        # build for statement
        condition = lcapi.builder().binary(to_lctype(bool), lcapi.BinaryOp.LESS, idxexpr, range_stop)
        forstmt = lcapi.builder().for_(idxexpr, condition, range_step)
        with forstmt.body():
            for x in node.body:
                build(x)

    @staticmethod
    def build_For(node):
        # currently only supports for x in range(...)
        assert type(node.target) is ast.Name
        if type(node.iter) is ast.Call and type(node.iter.func) is ast.Name and node.iter.func.id == "range":
            return build.build_range_for(node)
        build(node.iter)
        if type(node.iter.dtype) is ArrayType or node.iter.dtype in {*vector_dtypes, *matrix_dtypes}:
            return build.build_container_for(node)
        else:
            raise TypeError(f"{node.iter.dtype} object is not iterable")

    @staticmethod
    def build_While(node):
        loopstmt = lcapi.builder().loop_()
        with loopstmt.body():
            # condition
            build(node.test)
            ifstmt = lcapi.builder().if_(node.test.expr)
            with ifstmt.false_branch():
                lcapi.builder().break_()
            # body
            for x in node.body:
                build(x)

    @staticmethod
    def build_Break(node):
        lcapi.builder().break_()

    @staticmethod
    def build_Continue(node):
        lcapi.builder().continue_()

    @staticmethod
    def build_JoinedStr(node):
        node.joined = []
        for x in node.values:
            if isinstance(x, ast.FormattedValue):
                build(x.value)
                if hasattr(x.value, 'joined'):
                    node.joined += x.value.joined
                else:
                    node.joined.append(x.value)
            elif isinstance(x, ast.Constant):
                build(x)
                node.joined.append(x)
            else:
                assert False

    @staticmethod
    def build_List(node):
        node.dtype = list
        for x in node.elts:
            build(x)
        node.expr = None

    @staticmethod
    def build_Pass(node):
        pass
    
build = ASTVisitor()
