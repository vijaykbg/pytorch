import argparse
import os
import re
import yaml
from collections import OrderedDict, defaultdict
from itertools import groupby
from tools.shared.module_loader import import_module

CodeTemplate = import_module('code_template', 'torch/lib/ATen/code_template.py').CodeTemplate


# TODO: refactor nested_dict into common library with ATen
class nested_dict(object):
    def __init__(self, base, parent):
        self.base, self.parent = base, parent

    def __contains__(self, item):
        return item in self.base or item in self.parent

    def __getitem__(self, x):
        r = self.base.get(x)
        if r is not None:
            return r
        return self.parent[x]


try:
    # use faster C loader if available
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


METHOD_DECLARATION = CodeTemplate("""\
virtual ${return_type} ${method_prefix}${api_name}(${formals}) const override;
""")

METHOD_DEFINITION = CodeTemplate("""\
${return_type} VariableType::${method_prefix}${api_name}(${formals}) const {
    ${type_definition_body}
}
""")

METHOD_DEFINITION_NYI = CodeTemplate("""\
throw std::runtime_error("${api_name}: NYI");""")

METHOD_DEFINITION_FALLTHROUGH = CodeTemplate("""\
return baseType->${method_prefix}${api_name}(${unpacked_args});""")

METHOD_DEFINITION_FALLTHROUGH_VARIABLE = CodeTemplate("""\
return make_variable(baseType->${method_prefix}${api_name}(${unpacked_args}));""")

UNWRAP_TENSOR = CodeTemplate("""\
auto& ${arg_name}_ = checked_unpack(${arg_name}, "${arg_name}", ${arg_pos});""")

UNWRAP_TENSORLIST = CodeTemplate("""\
auto ${arg_name}_ = checked_unpack(${arg_name}, "${arg_name}", ${arg_pos});""")

FUNCTION_DECLARATION = CodeTemplate("""\
struct ${op} : public Function {
  using Function::Function;
  variable_list apply(const variable_list& inputs) override;
  std::string name() override { return "${op}"; }
  ${saved_variables}
};
""")

FUNCTION_DEFINITION = CodeTemplate("""\
variable_list ${op}::apply(const variable_list& inputs) {
  variable_list grad_inputs(${num_inputs});
  ${body}
  return grad_inputs;
}
""")

PY_FUNCTION_DEFINITION = CodeTemplate("""\
static PyTypeObject ${op}Class;
addClass<${op}>(${op}Class, "${op}");
""")


DERIVATIVE_TENSOR = CodeTemplate("""\
if (should_compute_output(${i})) {
  grad_inputs[${i}] = ${derivative};
}
""")

DERIVATIVE_TENSORLIST = CodeTemplate("""\
if (should_compute_any_outputs()) {
  grad_inputs = ${derivative};
}
""")

METHOD_DEFINITION_FLAGS_TENSORS = CodeTemplate("""\
   auto flags = Function::flags({ ${tensor_args} });
""")

METHOD_DEFINITION_FLAGS_TENSORLIST = CodeTemplate("""\
   auto flags = Function::flags( ${tensorlist_args});
""")

METHOD_DEFINITION_DERIVATIVE = CodeTemplate("""\
${flags_def}
auto grad_fn = std::make_shared<${op}>();
if (flags.is_executable) {
  ${save_variables}
}
auto output = as_variable(baseType->${method_prefix}${api_name}(${unpacked_args}));
wrap_output(*output.get(), std::move(flags), std::move(grad_fn));
return ${return_value};
""")

METHOD_DEFINITION_INPLACE = CodeTemplate("""\
auto& pImpl = static_cast<VariableImpl&>(*self.get());
check_inplace(pImpl);
${flags_def}
auto grad_fn = std::make_shared<${op}>();
if (flags.is_executable) {
  ${save_variables}
}
baseType->${method_prefix}${api_name}(${unpacked_args});
(*pImpl.version_counter)++;
wrap_output(pImpl, std::move(flags), std::move(grad_fn));
return self;
""")


PY_VARIABLE_CASE = CodeTemplate("""\
${cond} (r.idx == ${i}) {
  return wrap(dispatch_${name}(${args_with_self}));
""")

PY_VARIABLE_CASE_STATIC = CodeTemplate("""\
${cond} (r.idx == ${i}) {
  return wrap(dispatch_${name}(${args_without_self}));
""")

PY_VARIABLE_DISPATCH_TO_METHOD = CodeTemplate("""\
inline ${return_type} dispatch_${name}(${formal_args}) {
  ${AutoNoGIL}
  ${AutoGPU}
  return self.${name}(${dispatch_args});
}
""")

PY_VARIABLE_DISPATCH_TO_FUNCTION = CodeTemplate("""\
inline ${return_type} dispatch_${name}(${formal_args}) {
  ${AutoNoGIL}
  ${AutoGPU}
  return at::${name}(${dispatch_args});
}
""")

PY_VARIABLE_METHOD_NOARGS = CodeTemplate("""\
static PyObject * THPVariable_${name}(PyObject* self, PyObject* args)
{
  HANDLE_TH_ERRORS
  auto& self_ = reinterpret_cast<THPVariable*>(self)->cdata;
  return wrap(dispatch_${name}(self_));
  END_HANDLE_TH_ERRORS
}
""")

PY_VARIABLE_METHOD_VARARGS = CodeTemplate("""\
static PyObject * THPVariable_${name}(PyObject* self, PyObject* args, PyObject* kwargs)
{
  HANDLE_TH_ERRORS
  static PythonArgParser parser({
    ${prototypes}
  });
  auto& self_ = reinterpret_cast<THPVariable*>(self)->cdata;
  PyObject* parsed_args[${max_args}];
  auto r = parser.parse(args, kwargs, parsed_args);
  ${dispatch}
  Py_RETURN_NONE;
  END_HANDLE_TH_ERRORS
}
""")

PY_VARIABLE_METHOD_STATIC = CodeTemplate("""\
static PyObject * THPVariable_${name}(PyObject* self, PyObject* args, PyObject* kwargs)
{
  HANDLE_TH_ERRORS
  static PythonArgParser parser({
    ${prototypes}
  });
  PyObject* parsed_args[${max_args}];
  auto r = parser.parse(args, kwargs, parsed_args);
  ${dispatch}
  Py_RETURN_NONE;
  END_HANDLE_TH_ERRORS
}
""")

PY_VARIABLE_METHOD_DEF = CodeTemplate("""\
{"${name}", (PyCFunction)THPVariable_${name}, ${flags}, NULL},""")

GENERATED_COMMENT = CodeTemplate("""\
generated from tools/autograd/templates/${filename}""")

template_path = os.path.join(os.path.dirname(__file__), 'templates')

VARIABLE_TYPE_H = CodeTemplate.from_file(template_path + '/VariableType.h')
VARIABLE_TYPE_CPP = CodeTemplate.from_file(template_path + '/VariableType.cpp')
FUNCTIONS_H = CodeTemplate.from_file(template_path + '/Functions.h')
FUNCTIONS_CPP = CodeTemplate.from_file(template_path + '/Functions.cpp')
PY_VARIABLE_METHODS_CPP = CodeTemplate.from_file(template_path + '/python_variable_methods.cpp')
PY_VARIABLE_DISPATCH_H = CodeTemplate.from_file(template_path + '/python_variable_methods_dispatch.h')
PY_FUNCTIONS_H = CodeTemplate.from_file(template_path + '/python_functions.h')
PY_FUNCTIONS_CPP = CodeTemplate.from_file(template_path + '/python_functions.cpp')

derivatives_path = os.path.join(os.path.dirname(__file__), 'derivatives.yaml')
deprecated_path = os.path.join(os.path.dirname(__file__), 'deprecated.yaml')

# Functions with these return types delegate completely to the underlying
# base at::Type
FALLTHROUGH_RETURN_TYPES = {'int64_t', 'void*', 'bool', 'IntList'}
FALLTHROUGH_FUNCTIONS = {
    'eye', 'linspace', 'logspace', 'tensor', 'ones', 'ones_like', 'rand',
    'randn' 'randperm', 'range', 'tensor', 'zeros', 'zeros_like',
}


def format_return_type(returns):
    if len(returns) == 0:
        return 'void'
    elif len(returns) == 1:
        return returns[0]['type']
    else:
        return_types = [r['type'] for r in returns]
        return 'std::tuple<{}>'.format(','.join(return_types))


def write(dirname, name, template, env):
    env['generated_comment'] = GENERATED_COMMENT.substitute(filename=name)
    path = os.path.join(dirname, name)
    with open(path, 'w') as f:
        f.write(template.substitute(env))


def load_derivatives(path):
    with open(path, 'r') as f:
        definitions = yaml.load(f, Loader=Loader)

    # Matches "foo" in "foo, bar" but not "foobar". The name is substituted for
    # the {} characters.
    name_regex = r'(^|\W){}($|\W)'

    def split_name_params(prototype):
        name, params = re.match('(\w+)\((.*)\)', prototype).groups()
        return name, params.split(', ')

    def get_signature(option):
        arguments = option['python_arguments']
        arg_types = [arg['type'] for arg in arguments]
        if option['aten'] is not None:
            call_args = split_name_params(option['aten'])[1]
            arg_indices = {arg['name']: i for i, arg in enumerate(arguments)}

            def get_type(arg_name):
                if arg_name not in arg_indices:
                    # if the name is not an argument, assume it's a literal
                    # number, with type 'Scalar'
                    return 'Scalar'
                return arg_types[arg_indices[arg_name]]

            arg_types = [get_type(arg_name) for arg_name in call_args]
        return '{}({})'.format(option['name'], ', '.join(arg_types))

    # Parse each entry from derivatives.yaml
    options = []
    for defn in definitions:
        option = {}
        if '(' not in defn['name']:
            continue
        name, params = split_name_params(defn['name'])
        num_tensor_inputs = 0
        option['name'] = name
        option['aten'] = defn.get('aten')
        option['python_arguments'] = []
        option['prototype'] = defn['name']  # with default
        option['fallthrough'] = defn.get('fallthrough', False)
        option['op'] = name[0].upper() + name[1:] + 'Backward'

        arg_sizes_found = []
        derivatives = []
        for param in params:
            if param == '' or param == '*':
                continue
            arg = {}
            arg['type'], name = param.split(' ')
            if '=' in name:
                name, default = name.split('=')
                arg['optional'] = True
                arg['default'] = default
            arg['name'] = name
            option['python_arguments'].append(arg)

            if name in defn:
                saved = []
                formula = defn[name]
                for arg in option['python_arguments']:
                    size_str = arg['name'] + '.sizes()'
                    if size_str in formula:
                        sizes_name = arg['name'] + '_sizes'
                        formula = formula.replace(size_str, sizes_name)

                    # If x is a TensorList, turn x.sizes(y) into x_argsizes_y
                    def argsizes_repl(matchobj):
                        if arg['type'] != 'TensorList':
                            raise RuntimeError("sizes(argument) only supported on TensorList")
                        argsizes_name = arg['name'] + "_argsizes_" + matchobj.group(1)
                        arg_sizes_found.append(argsizes_name + ".size()")
                        return argsizes_name
                    formula = re.sub(arg['name'] + r".sizes\((\w+)\)", argsizes_repl, formula)

                    # If x is a Tensor, turn x.size(y) into x_argsize_y
                    def argsize_repl(matchobj):
                        if arg['type'] != 'Tensor':
                            raise RuntimeError("size(argument) only supported on Tensor")
                        argsize_name = arg['name'] + "_argsize_" + matchobj.group(1)
                        return argsize_name
                    formula = re.sub(arg['name'] + r".size\((\w+)\)", argsize_repl, formula)

                derivatives.append(formula)
                arg['derivative'] = formula
                if arg['type'] != "TensorList":
                    num_tensor_inputs += 1

        if arg_sizes_found:
            option['num_inputs'] = ("+".join(arg_sizes_found) +
                                    "" if num_tensor_inputs == 0 else " + " + str(num_tensor_inputs))
        else:
            option['num_inputs'] = str(num_tensor_inputs)

        if option['aten'] is not None:
            option['call_args'] = split_name_params(option['aten'])[1]
        else:
            option['call_args'] = [arg['name'] for arg in option['python_arguments']]
        option['signature'] = get_signature(option)

        saved = []
        for arg in option['python_arguments']:
            name = arg['name']
            sizes_name = name + '_sizes'
            if any(re.search(name_regex.format(name), f) for f in derivatives):
                saved.append(arg)
            if any(sizes_name in f for f in derivatives):
                saved.append({
                    'name': sizes_name,
                    'type': 'IntList',
                })
            for f in derivatives:
                for match_name in re.findall(r"{}_argsize_\w+".format(name), f):
                    saved.append({
                        'name': match_name,
                        'type': 'int64_t',
                    })
                for match_name in re.findall(r"{}_argsizes_\w+".format(name), f):
                    saved.append({
                        'name': match_name,
                        'type': 'IntList',
                    })
        option['saved'] = saved

        options.append(option)

    options = sorted(options, key=lambda o: o['name'])
    for name, overloads in groupby(options, lambda o: o['name']):
        overloads = list(overloads)
        for i, option in enumerate(overloads):
            name = option['name']
            option['op'] = name[0].upper() + name[1:] + 'Backward'
            if len(overloads) > 1:
                option['op'] += str(i)

    return options


def create_autograd_functions(top_env, declarations):
    """Functions.h and Functions.cpp body

    These contain the auto-generated subclasses of torch::autograd::Function
    for each every differentiable torch function.
    """
    function_definitions = top_env['autograd_function_definitions']
    function_declarations = top_env['autograd_function_declarations']
    py_function_initializers = top_env['py_function_initializers']

    def process_function(op):
        saved_variables = []
        for arg in op['saved']:
            name = arg['name']
            if arg['type'] == 'Tensor':
                saved_variables.append('SavedVariable {}_;'.format(name))
            elif arg['type'] == 'IntList':
                saved_variables.append('std::vector<int64_t> {};'.format(name))
            else:
                saved_variables.append('{} {};'.format(arg['type'], name))
        op['saved_variables'] = saved_variables

        body = []
        body.append('auto& grad = inputs[0];')

        def unpack_args():
            unpack = []
            for arg in op['saved']:
                if arg['type'] == 'Tensor':
                    name = arg['name']
                    unpack.append('auto {} = {}_.unpack();'.format(name, name))
            return unpack

        body.extend(unpack_args())

        i = 0
        added_derivative_tensor = False
        added_derivative_tensorlist = False
        for arg in op['python_arguments']:
            derivative = arg.get('derivative')
            if derivative is None:
                continue

            if arg['type'] == 'TensorList':
                if added_derivative_tensor:
                    raise RuntimeError("derivatives don't support specifying both a TensorList "
                                       "and non-TensorList derivative yet")
                added_derivative_tensorlist = True
                body.append(DERIVATIVE_TENSORLIST.substitute({
                    'i': i,
                    'derivative': derivative,
                }))
            else:
                if added_derivative_tensorlist:
                    raise RuntimeError("derivatives don't support specifying both a TensorList "
                                       "and non-TensorList derivative yet")
                added_derivative_tensor = True
                body.append(DERIVATIVE_TENSOR.substitute({
                    'i': i,
                    'derivative': derivative,
                }))
            i += 1

        op['body'] = body
        function_declarations.append(FUNCTION_DECLARATION.substitute(op))
        function_definitions.append(FUNCTION_DEFINITION.substitute(op))
        py_function_initializers.append(PY_FUNCTION_DEFINITION.substitute(op))

    for option in declarations:
        process_function(option)


def is_implemented(option):
    return (option['return_type'] in FALLTHROUGH_RETURN_TYPES or
            option['name'] in FALLTHROUGH_FUNCTIONS or
            option.get('derivative') is not None)


def create_variable_type(top_env, aten_declarations):
    """VariableType.h and VariableType.cpp body

    This is the at::Type subclass for differentiable tensors. The
    implementation of each function dispatches to the base tensor type to
    compute the output. The grad_fn is attached to differentiable functions.
    """

    type_declarations = top_env['type_derived_method_declarations']
    type_definitions = top_env['type_derived_method_definitions']

    def save_variables(option, derivative):
        # assign the saved variables to the generated grad_fn
        stmts = []
        for arg in derivative['saved']:
            name = arg['name']
            expr = arg['name']
            if '_sizes' in name:
                expr = name.replace('_sizes', '.sizes()')
            elif '_argsize_' in name:
                # turn x_argsizes_y into to_arg_sizes(x, y)
                expr = re.sub(r"(\w+)_argsize_(\w+)", r"\1.size(\2)", name)
            elif '_argsizes_' in name:
                # turn x_argsizes_y into to_arg_sizes(x, y)
                expr = re.sub(r"(\w+)_argsizes_(\w+)", r"to_arg_sizes(\1, \2)", name)
            elif arg['type'] == 'Tensor':
                name += '_'
                var = arg['name']
                if var == 'self' and option['inplace']:
                    var = 'self.clone()'
                expr = 'SavedVariable({}, nullptr)'.format(var)
            stmts.append('grad_fn->{} = {};'.format(name, expr))
        return stmts

    def unpack_args(env, option):
        body = []
        unpacked_args = []
        for i, arg in enumerate(option['arguments']):
            if arg['dynamic_type'] == 'Tensor':
                body.append(UNWRAP_TENSOR.substitute(arg_name=arg['name'], arg_pos=i))
                unpacked_args.append(arg['name'] + '_')
            elif arg['dynamic_type'] == 'TensorList':
                body.append(UNWRAP_TENSORLIST.substitute(arg_name=arg['name'], arg_pos=i))
                unpacked_args.append(arg['name'] + '_')
            else:
                unpacked_args.append(arg['name'])
        env['unpacked_args'] = unpacked_args
        return body

    def emit_body(env, option):
        if not is_implemented(option):
            return METHOD_DEFINITION_NYI.substitute(option)

        body = []
        body += unpack_args(env, option)

        combined = nested_dict(env, option)
        if option['return_type'] in FALLTHROUGH_RETURN_TYPES:
            body.extend(METHOD_DEFINITION_FALLTHROUGH.substitute(combined).split('\n'))
            return body
        elif option['derivative'] is None:
            assert option['name'] in FALLTHROUGH_FUNCTIONS
            body.extend(METHOD_DEFINITION_FALLTHROUGH_VARIABLE.substitute(combined).split('\n'))
            return body

        if combined['tensorlist_args']:
            flags_def = METHOD_DEFINITION_FLAGS_TENSORLIST.substitute(combined)
            if combined['tensor_args']:
                raise RuntimeError("both tensorlist_args and tensor_args not currently supported")
        else:
            flags_def = METHOD_DEFINITION_FLAGS_TENSORS.substitute(combined)
        if option['inplace']:
            body.extend(METHOD_DEFINITION_INPLACE.substitute(combined, flags_def=flags_def).split('\n'))
        else:
            body.extend(METHOD_DEFINITION_DERIVATIVE.substitute(combined, flags_def=flags_def).split('\n'))
        return body

    def process_function(option):
        env = {}
        if option.get('derivative') is not None:
            derivative = option['derivative']
            env['op'] = derivative['op']
            env['save_variables'] = save_variables(option, derivative)
            env['tensor_args'] = [arg['name'] for arg in option['arguments']
                                  if arg['dynamic_type'] == 'Tensor']
            env['tensorlist_args'] = [arg['name'] for arg in option['arguments']
                                      if arg['dynamic_type'] == 'TensorList']
        if option['return_type'] == 'Scalar':
            env['return_value'] = 'Scalar(output)'
        else:
            env['return_value'] = 'Tensor(std::move(output))'

        env['type_definition_body'] = emit_body(env, option)

        combined = nested_dict(env, option)
        type_declarations.append(METHOD_DECLARATION.substitute(combined))
        if option['name'] != 'resize_':
            type_definitions.append(METHOD_DEFINITION.substitute(combined))

    for function in aten_declarations:
        process_function(function)


def create_python_bindings(top_env, python_functions):
    """python_variable_methods.cpp

    Generates Python bindings to Variable methods
    """
    py_methods = top_env['py_methods']
    py_method_defs = top_env['py_method_defs']
    py_method_dispatch = top_env['py_method_dispatch']

    unpack_methods = {
        'int64_t': 'toInt64',
        'bool': 'toBool'
    }

    def args_without_self(args):
        return [arg for arg in args if arg['name'] != 'self']

    def emit_dispatch(i, option):
        env = {}

        args = []
        python_params = args_without_self(option['python_arguments'])
        has_self = any([True for arg in option['python_arguments'] if arg['name'] == 'self'])
        formal_args = ['Tensor & self'] if has_self else []
        for arg_idx, arg in enumerate(python_params):
            unpack = unpack_methods.get(arg['type'], arg['type'].lower())
            args.append('r.{}({})'.format(unpack, arg_idx))
            dispatch_type = arg['type']
            dispatch_type = 'const Tensor &' if dispatch_type == 'Tensor' else dispatch_type
            formal_args.append('{} {}'.format(dispatch_type, arg['name']))

        env['i'] = i
        env['dispatch_args'] = [arg for arg in option['call_args'] if arg != 'self']
        env['args_without_self'] = args
        env['args_with_self'] = ['self_'] + args
        env['AutoNoGIL'] = 'AutoNoGIL no_gil;'
        if has_self:
            env['AutoGPU'] = 'AutoGPU auto_gpu(self);'
        else:
            if len(python_params) == 0:
                raise RuntimeError("couldn't find argument for AutoGPU")
            env['AutoGPU'] = 'AutoGPU auto_gpu({});'.format(python_params[0]['name'])
        env['formal_args'] = formal_args
        env['cond'] = 'if' if i == 0 else '} else if'
        env = nested_dict(env, option)
        if has_self:
            py_method_dispatch.append(PY_VARIABLE_DISPATCH_TO_METHOD.substitute(env))
            return PY_VARIABLE_CASE.substitute(env)
        else:
            py_method_dispatch.append(PY_VARIABLE_DISPATCH_TO_FUNCTION.substitute(env))
            return PY_VARIABLE_CASE_STATIC.substitute(env)

    def process_option(name, options):
        env = {}
        env['name'] = name
        env['prototypes'] = []
        env['max_args'] = max(len(o['python_arguments']) for o in options)
        for o in options:
            prototype = o['prototype']
            if o['inplace']:
                prototype = prototype.replace('(', '_(')
            prototype = prototype.replace('Tensor self, ', '')
            prototype = prototype.replace('Tensor self', '')
            if 'deprecated' in o:
                prototype += '|deprecated'
            env['prototypes'].append('"{}",'.format(prototype))

        dispatch = []
        for i, option in enumerate(options):
            dispatch.append(emit_dispatch(i, nested_dict(env, option)))
        dispatch.append('}')
        env['dispatch'] = dispatch

        has_self = 'self' in options[0]['args']
        if len(options) == 1 and len(options[0]['args']) == 1:
            if has_self:
                tmpl = PY_VARIABLE_METHOD_NOARGS
                env['flags'] = 'METH_NOARGS'
            else:
                raise RuntimeError("static args method not yet implemented")
        else:
            if has_self:
                tmpl = PY_VARIABLE_METHOD_VARARGS
                env['flags'] = 'METH_VARARGS | METH_KEYWORDS'
            else:
                tmpl = PY_VARIABLE_METHOD_STATIC
                env['flags'] = 'METH_STATIC | METH_VARARGS | METH_KEYWORDS'

        py_methods.append(tmpl.substitute(env))
        py_method_defs.append(PY_VARIABLE_METHOD_DEF.substitute(env))

    for name, options in python_functions.items():
        process_option(name, options)


def gen_variable_type(declarations, out):
    with open(declarations, 'r') as f:
        aten_decls = [option for option in yaml.load(f, Loader=Loader)]

    derivatives = load_derivatives(derivatives_path)
    deprecated = load_derivatives(deprecated_path)

    def by_name(option):
        return option['name']

    def by_aten_name(option):
        return option.get('aten_name', option['name'])

    aten_decls = sorted(aten_decls, key=by_name)
    derivatives = sorted(derivatives, key=by_aten_name)

    derivatives_by_signature = {d['signature']: d for d in derivatives}
    options_by_name = OrderedDict([(k, list(g)) for k, g in groupby(aten_decls, by_name)])
    options_by_signature = OrderedDict()
    python_functions = OrderedDict()

    def get_option(derivative):
        name = derivative.get('aten', derivative['name'])
        options = options_by_name.get(name, [])
        if len(options) == 0:
            raise RuntimeError('Declaration not found for: {}'.format(name))
        elif len(options) == 1:
            return options[0]
        else:
            raise RuntimeError('ambiguous decl for: {}'.format(name))
            return None

    for option in aten_decls:
        args = []
        for arg in option['arguments']:
            simple_type = arg['type'].replace(' &', '').replace('const ', '')
            args.append(simple_type)
            arg['simple_type'] = simple_type
        name = option['name']
        base_name = name[:-1] if option['inplace'] else name
        signature = '{}({})'.format(base_name, ', '.join(args))
        if signature not in options_by_signature:
            options_by_signature[signature] = []

        option['formals'] = [arg['type'] + ' ' + arg['name']
                             for arg in option['arguments']]
        option['args'] = [arg['name'] for arg in option['arguments']]
        option['api_name'] = option['name']
        option['return_type'] = format_return_type(option['returns'])

        options_by_signature[signature].append(option)
        derivative = derivatives_by_signature.get(signature)
        option['derivative'] = derivative
        if derivative is not None:
            if name not in python_functions:
                python_functions[name] = []
            python_functions[name].append(nested_dict(derivative, option))

    for declaration in deprecated:
        name = declaration['name']
        declaration['deprecated'] = True
        options = options_by_signature.get(declaration['signature'])
        if options is not None:
            python_functions[name].append(nested_dict(declaration, options[0]))

    env = {
        'autograd_function_declarations': [],
        'autograd_function_definitions': [],
        'type_derived_method_declarations': [],
        'type_derived_method_definitions': [],
        'py_methods': [],
        'py_method_defs': [],
        'py_method_dispatch': [],
        'py_function_initializers': [],
    }

    create_autograd_functions(env, derivatives)
    create_variable_type(env, aten_decls)
    create_python_bindings(env, python_functions)

    write(out, 'VariableType.h', VARIABLE_TYPE_H, env)
    write(out, 'VariableType.cpp', VARIABLE_TYPE_CPP, env)
    write(out, 'Functions.h', FUNCTIONS_H, env)
    write(out, 'Functions.cpp', FUNCTIONS_CPP, env)
    write(out, 'python_variable_methods.cpp', PY_VARIABLE_METHODS_CPP, env)
    write(out, 'python_variable_methods_dispatch.h', PY_VARIABLE_DISPATCH_H, env)
    write(out, 'python_functions.h', PY_FUNCTIONS_H, env)
    write(out, 'python_functions.cpp', PY_FUNCTIONS_CPP, env)


def main():
    parser = argparse.ArgumentParser(
        description='Generate autograd C++ files script')
    parser.add_argument('declarations', metavar='DECL',
                        help='path to Declarations.yaml')
    parser.add_argument('out', metavar='OUT',
                        help='path to output directory')
    args = parser.parse_args()
    gen_variable_type(args.declarations, args.out)


if __name__ == '__main__':
    main()
