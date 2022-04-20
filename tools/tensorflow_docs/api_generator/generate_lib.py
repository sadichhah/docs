# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Generate tensorflow.org style API Reference docs for a Python module."""

import collections
import os
import pathlib
import shutil
import tempfile

from typing import Any, Dict, List, Optional, Sequence, Tuple, Type, Union

from tensorflow_docs.api_generator import config
from tensorflow_docs.api_generator import doc_generator_visitor
from tensorflow_docs.api_generator import parser
from tensorflow_docs.api_generator import public_api
from tensorflow_docs.api_generator import reference_resolver as reference_resolver_lib
from tensorflow_docs.api_generator import toc as toc_lib
from tensorflow_docs.api_generator import traverse

from tensorflow_docs.api_generator.pretty_docs import docs_for_object

from tensorflow_docs.api_generator.report import utils

import yaml

# Used to add a collections.OrderedDict representer to yaml so that the
# dump doesn't contain !!OrderedDict yaml tags.
# Reference: https://stackoverflow.com/a/21048064
# Using a normal dict doesn't preserve the order of the input dictionary.
_mapping_tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG


def dict_representer(dumper, data):
  return dumper.represent_dict(data.items())


def dict_constructor(loader, node):
  return collections.OrderedDict(loader.construct_pairs(node))


yaml.add_representer(collections.OrderedDict, dict_representer)
yaml.add_constructor(_mapping_tag, dict_constructor)


def write_docs(
    *,
    output_dir: Union[str, pathlib.Path],
    parser_config: config.ParserConfig,
    yaml_toc: Union[bool, Type[toc_lib.TocBuilder]],
    root_module_name: str,
    root_title: str = 'TensorFlow',
    search_hints: bool = True,
    site_path: str = 'api_docs/python',
    gen_redirects: bool = True,
    gen_report: bool = True,
    extra_docs: Optional[Dict[int, str]] = None,
    page_builder_classes: Optional[docs_for_object.PageBuilderDict] = None,
):
  """Write previously extracted docs to disk.

  Write a docs page for each symbol included in the indices of parser_config to
  a tree of docs at `output_dir`.

  Symbols with multiple aliases will have only one page written about
  them, which is referenced for all aliases.

  Args:
    output_dir: Directory to write documentation markdown files to. Will be
      created if it doesn't exist.
    parser_config: A `config.ParserConfig` object, containing all the necessary
      indices.
    yaml_toc: Set to `True` to generate a "_toc.yaml" file.
    root_module_name: (str) the name of the root module (`tf` for tensorflow).
    root_title: The title name for the root level index.md.
    search_hints: (bool) include meta-data search hints at the top of each
      output file.
    site_path: The output path relative to the site root. Used in the
      `_toc.yaml` and `_redirects.yaml` files.
    gen_redirects: Bool which decides whether to generate _redirects.yaml file
      or not.
    gen_report: If True, a report for the library is generated by linting the
      docstrings of its public API symbols.
    extra_docs: To add docs for a particular object instance set it's __doc__
      attribute. For some classes (list, tuple, etc) __doc__ is not writable.
      Pass those docs like: `extra_docs={id(obj): "docs"}`
    page_builder_classes: A optional dict of `{ObjectType:Type[PageInfo]}` for
        overriding the default page builder classes.

  Raises:
    ValueError: if `output_dir` is not an absolute path
  """
  output_dir = pathlib.Path(output_dir)
  site_path = pathlib.Path('/', site_path)

  # Make output_dir.
  if not output_dir.is_absolute():
    raise ValueError("'output_dir' must be an absolute path.\n"
                     f"    output_dir='{output_dir}'")
  output_dir.mkdir(parents=True, exist_ok=True)

  # Collect redirects for an api _redirects.yaml file.
  redirects = []

  api_report = None
  if gen_report:
    api_report = utils.ApiReport()

  # Parse and write Markdown pages, resolving cross-links (`tf.symbol`).
  num_docs_output = 0
  for node in parser_config.api_tree.iter_nodes():
    full_name = node.full_name
    py_object = node.py_object

    if node.output_type() is node.OutputType.FRAGMENT:
      continue

    # Generate docs for `py_object`, resolving references.
    try:
      page_info = docs_for_object.docs_for_object(
          full_name=full_name,
          py_object=py_object,
          parser_config=parser_config,
          extra_docs=extra_docs,
          search_hints=search_hints,
          page_builder_classes=page_builder_classes)

      if api_report is not None and not full_name.startswith(
          ('tf.compat.v', 'tf.keras.backend', 'tf.numpy',
           'tf.experimental.numpy')):
        api_report.fill_metrics(page_info)
    except Exception as e:
      raise ValueError(
          f'Failed to generate docs for symbol: `{full_name}`') from e

    path = output_dir / parser.documentation_path(full_name)

    try:
      path.parent.mkdir(exist_ok=True, parents=True)
      path.write_text(page_info.page_text, encoding='utf-8')
      num_docs_output += 1
    except OSError as e:
      raise OSError('Cannot write documentation for '
                    f'{full_name} to {path.parent}') from e

    duplicates = parser_config.duplicates.get(full_name, [])
    if not duplicates:
      continue

    duplicates = [item for item in duplicates if item != full_name]

    if gen_redirects:
      for dup in duplicates:
        from_path = site_path / dup.replace('.', '/')
        to_path = site_path / full_name.replace('.', '/')
        redirects.append({'from': str(from_path), 'to': str(to_path)})

  if api_report is not None:
    api_report.write(output_dir / root_module_name / 'api_report.pb')


  if num_docs_output <= 1:
    raise ValueError('The `DocGenerator` failed to generate any docs. Verify '
                     'your arguments (`base_dir` and `callbacks`). '
                     'Everything you want documented should be within '
                     '`base_dir`.')

  if yaml_toc:
    if isinstance(yaml_toc, bool):
      yaml_toc = toc_lib.FlatModulesTocBuilder
    toc = yaml_toc(site_path).build(parser_config.api_tree)

    toc_path = output_dir / root_module_name / '_toc.yaml'
    toc.write(toc_path)

  if redirects and gen_redirects:
    redirects_dict = {
        'redirects': sorted(redirects, key=lambda redirect: redirect['from'])
    }

    api_redirects_path = output_dir / root_module_name / '_redirects.yaml'
    with open(api_redirects_path, 'w') as redirect_file:
      yaml.dump(redirects_dict, redirect_file, default_flow_style=False)

  # Write a global index containing all full names with links.
  with open(output_dir / root_module_name / 'all_symbols.md', 'w') as f:
    global_index = parser.generate_global_index(
        root_title, parser_config.index, parser_config.reference_resolver)
    if not search_hints:
      global_index = 'robots: noindex\n' + global_index
    f.write(global_index)


def add_dict_to_dict(add_from, add_to):
  for key in add_from:
    if key in add_to:
      add_to[key].extend(add_from[key])
    else:
      add_to[key] = add_from[key]


def extract(
    py_modules,
    base_dir,
    private_map: Dict[str, Any],
    visitor_cls: Type[
        doc_generator_visitor.DocGeneratorVisitor] = doc_generator_visitor
    .DocGeneratorVisitor,
    callbacks: Optional[public_api.ApiFilter] = None,
    include_default_callbacks=True):
  """Walks the module contents, returns an index of all visited objects.

  The return value is an instance of `self._visitor_cls`, usually:
  `doc_generator_visitor.DocGeneratorVisitor`

  Args:
    py_modules: A list containing a single (short_name, module_object) pair.
      like `[('tf',tf)]`.
    base_dir: The package root directory. Nothing defined outside of this
      directory is documented.
    private_map: A {'path':["name"]} dictionary listing particular object
      locations that should be ignored in the doc generator.
    visitor_cls: A class, typically a subclass of
      `doc_generator_visitor.DocGeneratorVisitor` that acumulates the indexes of
      objects to document.
    callbacks: Additional callbacks passed to `traverse`. Executed between the
      `PublicApiFilter` and the accumulator (`DocGeneratorVisitor`). The
      primary use case for these is to filter the list of children (see:
      `public_api.local_definitions_filter`)
    include_default_callbacks: When true the long list of standard
      visitor-callbacks are included. When false, only the `callbacks` argument
      is used.

  Returns:
    The accumulator (`DocGeneratorVisitor`)
  """
  if callbacks is None:
    callbacks = []

  if len(py_modules) != 1:
    raise ValueError("only pass one [('name',module)] pair in py_modules")
  short_name, py_module = py_modules[0]

  # The objects found during traversal, and their children are passed to each
  # of these filters in sequence. Each visitor returns the list of children
  # to be passed to the next visitor.
  if include_default_callbacks:
    filters = [
        # filter the api.
        public_api.FailIfNestedTooDeep(10),
        public_api.filter_module_all,
        public_api.add_proto_fields,
        public_api.filter_builtin_modules,
        public_api.filter_private_symbols,
        public_api.FilterBaseDirs(base_dir),
        public_api.FilterPrivateMap(private_map),
        public_api.filter_doc_controls_skip,
        public_api.ignore_typing
    ]
  else:
    filters = []

  accumulator = visitor_cls()
  traverse.traverse(
      py_module, filters + callbacks, accumulator, root_name=short_name)

  accumulator.build()
  return accumulator


EXCLUDED = set(['__init__.py', 'OWNERS', 'README.txt'])


class DocGenerator:
  """Main entry point for generating docs."""

  def __init__(
      self,
      root_title: str,
      py_modules: Sequence[Tuple[str, Any]],
      base_dir: Optional[Sequence[Union[str, pathlib.Path]]] = None,
      code_url_prefix: Union[Optional[str], Sequence[Optional[str]]] = (),
      search_hints: bool = True,
      site_path: str = 'api_docs/python',
      private_map: Optional[Dict[str, str]] = None,
      visitor_cls: Type[
          doc_generator_visitor.DocGeneratorVisitor] = doc_generator_visitor
      .DocGeneratorVisitor,
      api_cache: bool = True,
      callbacks: Optional[List[public_api.ApiFilter]] = None,
      yaml_toc: Union[bool, Type[toc_lib.TocBuilder]] = True,
      gen_redirects: bool = True,
      gen_report: bool = True,
      extra_docs: Optional[Dict[int, str]] = None,
      page_builder_classes: Optional[docs_for_object.PageBuilderDict] = None,
  ):
    """Creates a doc-generator.

    Args:
      root_title: A string. The main title for the project. Like "TensorFlow"
      py_modules: The python module to document.
      base_dir: String or tuple of strings. Directories that "Defined in" links
        are generated relative to. **Modules outside one of these directories
        are not documented**. No `base_dir` should be inside another.
      code_url_prefix: String or tuple of strings. The prefix to add to "Defined
        in" paths. These are zipped with `base-dir`, to set the `defined_in`
        path for each file. The defined in link for `{base_dir}/path/to/file` is
        set to `{code_url_prefix}/path/to/file`.
      search_hints: Bool. Include metadata search hints at the top of each file.
      site_path: Path prefix in the "_toc.yaml"
      private_map: DEPRECATED. Use `api_generator.doc_controls`, or pass a
        filter to the `callbacks` argument. A
        `{"module.path.to.object": ["names"]}` dictionary. Specific
        aliases that should not be shown in the resulting docs.
      visitor_cls: An option to override the default visitor class
        `doc_generator_visitor.DocGeneratorVisitor`.
      api_cache: Bool. Generate an api_cache file. This is used to easily add
        api links for backticked symbols (like `tf.add`) in other docs.
      callbacks: Additional callbacks passed to `traverse`. Executed between the
        `PublicApiFilter` and the accumulator (`DocGeneratorVisitor`). The
        primary use case for these is to filter the list of children (see:
        `public_api.ApiFilter` for the required signature)
      yaml_toc: Bool which decides whether to generate _toc.yaml file or not.
      gen_redirects: Bool which decides whether to generate _redirects.yaml file
        or not.
      gen_report: If True, a report for the library is generated by linting the
        docstrings of its public API symbols.
      extra_docs: To add docs for a particular object instance set it's __doc__
        attribute. For some classes (list, tuple, etc) __doc__ is not writable.
        Pass those docs like: `extra_docs={id(obj): "docs"}`
      page_builder_classes: An optional dict of `{ObjectType:Type[PageInfo]}`
        for overriding the default page builder classes.
    """
    self._root_title = root_title
    self._py_modules = py_modules
    self._short_name = py_modules[0][0]
    self._py_module = py_modules[0][1]

    if base_dir is None:
      # Determine the base_dir for the module
      base_dir = public_api.get_module_base_dirs(self._py_module)
    else:
      if isinstance(base_dir, (str, pathlib.Path)):
        base_dir = (base_dir,)
      base_dir = tuple(pathlib.Path(d) for d in base_dir)
    self._base_dir = base_dir

    if not self._base_dir:
      raise ValueError('`base_dir` cannot be empty')

    if isinstance(code_url_prefix, str) or code_url_prefix is None:
      code_url_prefix = (code_url_prefix,)
    self._code_url_prefix = tuple(code_url_prefix)
    if not self._code_url_prefix:
      raise ValueError('`code_url_prefix` cannot be empty')

    if len(self._code_url_prefix) != len(base_dir):
      raise ValueError('The `base_dir` list should have the same number of '
                       'elements as the `code_url_prefix` list (they get '
                       'zipped together).')

    self._search_hints = search_hints
    self._site_path = site_path
    self._private_map = private_map or {}
    self._visitor_cls = visitor_cls
    self.api_cache = api_cache
    if callbacks is None:
      callbacks = []
    self._callbacks = callbacks
    self._yaml_toc = yaml_toc
    self._gen_redirects = gen_redirects
    self._gen_report = gen_report
    self._extra_docs = extra_docs
    self._page_builder_classes = page_builder_classes

  def make_reference_resolver(self, visitor):
    return reference_resolver_lib.ReferenceResolver.from_visitor(
        visitor, py_module_names=[self._short_name])

  def make_parser_config(self,
                         visitor: doc_generator_visitor.DocGeneratorVisitor):
    reference_resolver = self.make_reference_resolver(visitor)
    return config.ParserConfig(
        reference_resolver=reference_resolver,
        duplicates=visitor.duplicates,
        duplicate_of=visitor.duplicate_of,
        tree=visitor.tree,
        index=visitor.index,
        reverse_index=visitor.reverse_index,
        path_tree=visitor.path_tree,
        api_tree=visitor.api_tree,
        base_dir=self._base_dir,
        code_url_prefix=self._code_url_prefix)

  def run_extraction(self):
    """Walks the module contents, returns an index of all visited objects.

    The return value is an instance of `self._visitor_cls`, usually:
    `doc_generator_visitor.DocGeneratorVisitor`

    Returns:
    """
    visitor = extract(
        py_modules=self._py_modules,
        base_dir=self._base_dir,
        private_map=self._private_map,
        visitor_cls=self._visitor_cls,
        callbacks=self._callbacks)

    # Write the api docs.
    parser_config = self.make_parser_config(visitor)
    return parser_config

  def build(self, output_dir):
    """Build all the docs.

    This produces python api docs:
      * generated from `py_module`.
      * written to '{output_dir}/api_docs/python/'

    Args:
      output_dir: Where to write the resulting docs.
    """
    workdir = pathlib.Path(tempfile.mkdtemp())

    # Extract the python api from the _py_modules
    parser_config = self.run_extraction()
    work_py_dir = workdir / 'api_docs/python'
    write_docs(
        output_dir=str(work_py_dir),
        parser_config=parser_config,
        yaml_toc=self._yaml_toc,
        root_title=self._root_title,
        root_module_name=self._short_name.replace('.', '/'),
        search_hints=self._search_hints,
        site_path=self._site_path,
        gen_redirects=self._gen_redirects,
        gen_report=self._gen_report,
        extra_docs=self._extra_docs,
        page_builder_classes=self._page_builder_classes,
    )

    if self.api_cache:
      parser_config.reference_resolver.to_json_file(
          str(work_py_dir / self._short_name.replace('.', '/') /
              '_api_cache.json'))

    os.makedirs(output_dir, exist_ok=True)

    # Typical results are something like:
    #
    # out_dir/
    #    {short_name}/
    #    _redirects.yaml
    #    _toc.yaml
    #    api_report.pb
    #    index.md
    #    {short_name}.md
    #
    # Copy the top level files to the `{output_dir}/`, delete and replace the
    # `{output_dir}/{short_name}/` directory.

    for work_path in work_py_dir.glob('*'):
      out_path = pathlib.Path(output_dir) / work_path.name
      out_path.parent.mkdir(exist_ok=True, parents=True)

      if work_path.is_file():
        shutil.copy2(work_path, out_path)
      elif work_path.is_dir():
        shutil.rmtree(out_path, ignore_errors=True)
        shutil.copytree(work_path, out_path)
