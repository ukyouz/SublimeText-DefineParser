import glob
import html
import io
import os
import pickle
import re
import threading

# import functools
from collections import OrderedDict, namedtuple
from contextlib import contextmanager
from pprint import pformat, pprint

import sublime
import sublime_plugin

DEFINE = namedtuple("DEFINE", ("name", "params", "token", "line"))
TOKEN = namedtuple("DEFINE", ("name", "params", "line"))

REGEX_TOKEN = r"\b(?P<NAME>[a-zA-Z_][a-zA-Z0-9_]+)\b"
REGEX_DEFINE = (
    r"#define\s+"
    + REGEX_TOKEN
    + r"(?P<HAS_PAREN>\((?P<PARAMS>[\w, ]*)\))*\s*(?P<TOKEN>.+)*"
)
REGEX_UNDEF = r"#undef\s+" + REGEX_TOKEN
REGEX_INCLUDE = r'#include\s+["<](?P<PATH>.+)[">]\s*'
BIT = lambda n: 1 << n


def convertall_dec2fmt(text, fmt="0x{:X}"):
    re_sub_dec2hex = lambda m: "{}".format(fmt).format(int(m.group(1)))
    return re.sub(r"\b([0-9]+)\b", re_sub_dec2hex, text)


def glob_recursive(directory, ext=".c"):
    return [
        os.path.join(root, filename)
        for root, dirnames, filenames in os.walk(directory)
        for filename in filenames
        if filename.endswith(ext)
    ]


class DuplicatedIncludeError(Exception):
    """assert when parser can not found ONE valid include header file."""


class Parser:
    debug = False
    iterate = 0

    def __init__(self):
        self.reset()

    def reset(self):
        self.defs = OrderedDict()  # dict of DEFINE
        self.folder = ""

    def _debug_log(self, msg, *args):
        if self.debug:
            print(msg % args)

    def insert_define(self, name, *, params=None, token=None):
        """params: list of parameters required, token: define body"""
        new_params = params or []
        new_token = token or ""
        self.defs[name] = DEFINE(
            name=name,
            params=new_params,
            token=new_token,
            line="",
        )

    def remove_define(self, name):
        if name not in self.defs:
            raise KeyError("token '{}' is not defined!".format(name))

        del self.defs[name]

    def strip_token(self, token, reserve_whitespace=False):
        if token == None:
            return None
        if reserve_whitespace:
            token = token.rstrip()
        else:
            token = token.strip()
        inline_comment_regex = r"\/\*[^\/]+\*\/"
        comments = list(re.finditer(inline_comment_regex, token))
        if len(comments):
            for match in comments:
                token = token.replace(match.group(0), "")
        return token

    def try_eval_num(self, token):
        REG_LITERALS = [
            r"\b(?P<NUM>[0-9]+)([ul]|ull?|ll?u|ll)\b",
            r"\b(?P<NUM>0b[01]+)([ul]|ull?|ll?u|ll)\b",
            r"\b(?P<NUM>0[0-7]+)([ul]|ull?|ll?u|ll)\b",
            r"\b(?P<NUM>0x[0-9a-f]+)([ul]|ull?|ll?u|ll)\b",
        ]
        # remove integer literals type hint
        for reg in REG_LITERALS:
            for match in re.finditer(reg, token, re.IGNORECASE):
                literal_integer = match.group(0)
                number = match.group("NUM")
                token = token.replace(literal_integer, number)
        # calculate size of special type
        # transform type cascading to bit mask for equivalence calculation
        for sz_log2, special_type in enumerate(("U8", "U16", "U32", "U64")):
            # limitation:
            #   for equation like (U32)1 << (U32)(15) may be calculated to wrong value
            #   due to operator order
            data_sz = 2 ** sz_log2
            # sizeof(U16) -> 2
            token = re.sub(
                r"sizeof\(\s*{}\s*\)".format(special_type), str(data_sz), token
            )
            # (U16)x -> 0xFFFF & x
            token = re.sub(
                r"\(\s*{}\s*\)".format(special_type),
                "0x" + "F" * data_sz * 2 + " & ",
                token,
            )
        try:
            # syntax translation from C -> Python
            token = token.replace("/", "//")
            token = token.replace("&&", " and ")
            token = token.replace("||", " or ")
            return int(eval(token))
        except:
            return None

    def read_file_lines(
        self,
        fileio,
        func,
        try_if_else=True,
        ignore_header_guard=False,
        reserve_whitespace=False,
    ):
        regex_line_break = r"\\\s*$"
        regex_line_comment = r"\s*\/\/.*$"

        if_depth = 0
        if_true_bmp = 1  # bitmap for every #if statement
        if_done_bmp = 1  # bitmap for every #if statement
        first_guard_token = True
        is_block_comment = False
        # with open(filepath, "r", errors="replace") as fs:
        multi_lines = ""
        for line_no, line in enumerate(fileio.readlines(), 1):

            if not is_block_comment:
                if "/*" in line:  # start of block comment
                    block_comment_start = line.index("/*")
                    is_block_comment = "*/" not in line
                    block_comment_ending = (
                        line.index("*/") + 2 if not is_block_comment else len(line)
                    )
                    line = line[:block_comment_start] + line[block_comment_ending:]
                    if is_block_comment:
                        multi_lines += line

            if is_block_comment:
                if "*/" in line:  # end of block comment
                    line = line[line.index("*/") + 2 :]
                    is_block_comment = False
                else:
                    continue

            line = re.sub(
                regex_line_comment, "", self.strip_token(line, reserve_whitespace)
            )

            if try_if_else:
                match_if = re.match(r"#if((?P<NOT>n*)def)*\s*(?P<TOKEN>.+)", line)
                match_elif = re.match(r"#elif\s*(?P<TOKEN>.+)", line)
                match_else = re.match(r"#else.*", line)
                match_endif = re.match(r"#endif.*", line)
                if match_if:
                    if_depth += 1
                    token = match_if.group("TOKEN")
                    if_token = (
                        "0"  # header guard always uses #ifndef *
                        if ignore_header_guard
                        and first_guard_token
                        and (match_if.group("NOT") == "n")
                        else self.expand_token(
                            token,
                            try_if_else,
                            raise_key_error=False,
                            zero_undefined=True,
                        )
                    )
                    if_token_val = bool(self.try_eval_num(if_token))
                    if_true_bmp |= BIT(if_depth) * (
                        if_token_val ^ (match_if.group("NOT") == "n")
                    )
                    first_guard_token = (
                        False if match_if.group("NOT") == "n" else first_guard_token
                    )
                elif match_elif:
                    if_token = self.expand_token(
                        match_elif.group("TOKEN"),
                        try_if_else,
                        raise_key_error=False,
                        zero_undefined=True,
                    )
                    if_token_val = bool(self.try_eval_num(if_token))
                    if_true_bmp |= BIT(if_depth) * if_token_val
                    if_true_bmp &= ~(BIT(if_depth) & if_done_bmp)
                elif match_else:
                    if_true_bmp ^= BIT(if_depth)  # toggle state
                    if_true_bmp &= ~(BIT(if_depth) & if_done_bmp)
                elif match_endif:
                    if_true_bmp &= ~BIT(if_depth)
                    if_done_bmp &= ~BIT(if_depth)
                    if_depth -= 1

            multi_lines += re.sub(regex_line_break, "", line)
            if re.search(regex_line_break, line):
                if reserve_whitespace:
                    func(line, line_no)
                continue
            single_line = re.sub(regex_line_break, "", multi_lines)
            if if_true_bmp == BIT(if_depth + 1) - 1:
                func(single_line, line_no)
                if_done_bmp |= BIT(if_depth)
            elif try_if_else and (match_if or match_elif or match_else or match_endif):
                func(single_line, line_no)
            multi_lines = ""

    def _get_define(self, line):
        match = re.match(REGEX_UNDEF, line)
        if match is not None:
            name = match.group("NAME")
            if name in self.defs:
                del self.defs[name]
            return

        match = re.match(REGEX_DEFINE, line)
        if match == None:
            return

        name = match.group("NAME")
        parentheses = match.group("HAS_PAREN")
        params = match.group("PARAMS")
        param_list = [p.strip() for p in params.split(",")] if params else []
        match_token = match.group("TOKEN")
        token = self.strip_token(match_token) or "(1)"

        """
        #define AAA     // params = None
        #define BBB()   // params = []
        #define CCC(a)  // params = ['a']
        """
        return DEFINE(
            name=name,
            params=param_list if parentheses else None,
            token=token,
            line=line,
        )

    def read_folder_h(self, directory, try_if_else=True):
        self.folder = directory

        header_files = glob_recursive(directory, ".h")
        print("read_header cnt: ", len(header_files))

        header_done = set()
        pre_defined_keys = self.defs.keys()

        def get_included_file(path, src_file):
            path = os.path.normpath(path)
            src_file = os.path.normpath(src_file)
            included_files = [
                h
                for h in header_files
                if path in h and os.path.basename(path) == os.path.basename(h)
            ]
            if len(included_files) > 1:
                included_files = [
                    f for f in included_files if f.replace(path, "") in src_file
                ]

            if len(included_files) > 1:
                raise DuplicatedIncludeError(
                    pformat(included_files, indent=4, width=120)
                )

            return included_files[0] if len(included_files) else None

        def read_header(filepath):
            if filepath == None or filepath in header_done:
                return

            def insert_def(line, _):
                match_include = re.match(REGEX_INCLUDE, line)
                if match_include != None:
                    # parse included file first
                    path = match_include.group("PATH")
                    included_file = get_included_file(path, src_file=filepath)
                    read_header(included_file)
                define = self._get_define(line)
                if define == None or define.name in pre_defined_keys:
                    return
                self.defs[define.name] = define

            try:
                with open(filepath, "r", errors="replace") as fs:
                    self.read_file_lines(fs, insert_def, try_if_else)
            except UnicodeDecodeError as e:
                print("Fail to open {!r}. {}".format(filepath, e))

            if filepath in header_files:
                self._debug_log("Read File: %s", filepath)
                header_done.add(filepath)

        for header_file in header_files:
            read_header(header_file)

        return True

    def read_h(self, filepath, try_if_else=True):
        def insert_def(line, _):
            define = self._get_define(line)
            if define == None:
                return
            # if len(define.params):
            #     return
            self.defs[define.name] = define

        try:
            with open(filepath, "r", errors="replace") as fs:
                self.read_file_lines(fs, insert_def, try_if_else)
        except UnicodeDecodeError as e:
            print("Fail to open :{}. {}".format(filepath, e))

    @contextmanager
    def read_c(self, filepath, try_if_else=True):
        """use `with` context manager for having temporary tokens defined in .c source file"""

        defs = {}

        def insert_def(line, _):
            define = self._get_define(line)
            if define == None:
                return
            # if len(define.params):
            #     return
            defs[define.name] = define

        try:
            with open(filepath, "r", errors="replace") as fs:
                self.read_file_lines(fs, insert_def, try_if_else)
            for define in defs.values():
                self.insert_define(
                    name=define.name,
                    params=define.params,
                    token=define.token,
                )
            yield
        except UnicodeDecodeError as e:
            print("Fail to open :{}. {}".format(filepath, e))
        finally:
            for define in defs.values():
                del self.defs[define.name]

    def find_tokens(self, token):
        def fine_token_params(params):
            if len(params) and params[0] != "(":
                return None
            # (() ())
            brackets = 0
            new_params = ""
            for c in params:
                brackets += (c == "(") * 1 + (c == ")") * -1
                new_params += c
                if brackets == 0:
                    break
            return new_params

        if self.try_eval_num(token):
            return []

        # remove string value in token
        regex_str = r'"[^"]+"'
        token = re.sub(regex_str, "", token)

        tokens = list(re.finditer(REGEX_TOKEN, token))
        if len(tokens):
            ret_tokens = []
            for match in tokens:
                _token = match.group("NAME")
                params = None
                if _token in self.defs:
                    params_required = self.defs[_token].params
                    end_pos = match.end()
                    if params_required is not None:
                        params = fine_token_params(token[end_pos:])
                param_str = params if params else ""
                ret_tokens.append(
                    TOKEN(name=_token, params=params, line=_token + param_str)
                )
            return ret_tokens
        else:
            return []

    def _check_parentheses(self, token):
        lparan_cnt = 0
        rparan_cnt = 0
        for char in token:
            if char == "(":
                lparan_cnt += 1
            if char == ")":
                rparan_cnt += 1
        return lparan_cnt == rparan_cnt

    def _iter_arg(self, params):
        if len(params) == 0:
            return []
        assert params[0] == "(" and params[-1] == ")"
        parma_list = params[1:-1].split(",")
        arguments = []
        for arg in parma_list:
            arguments.append(arg.strip())
            prams_str = ",".join(arguments)
            if self._check_parentheses(prams_str):
                yield prams_str
                arguments = []

    # @functools.lru_cache
    def expand_token(
        self, token, try_if_else=True, raise_key_error=True, zero_undefined=False
    ):
        expanded_token = self.strip_token(token)
        self.iterate += 1

        word_boundary = lambda word: r"\b(##)*%s\b" % re.escape(word)
        tokens = self.find_tokens(expanded_token)
        for _token in tokens:
            name = _token.name
            params = self.strip_token(_token.params)
            if params is not None:
                # Expand all the parameters first
                for p_tok in self.find_tokens(params):
                    params = re.sub(
                        word_boundary(p_tok.line),
                        self.expand_token(p_tok.line, try_if_else, raise_key_error),
                        params,
                    )
                    processed = list(t for t in tokens if p_tok.name == t.name)
                    if len(processed):
                        tokens.remove(processed[0])
                if name in self.defs:
                    old_params = self.defs[name].params or []
                    new_params = list(self._iter_arg(params))
                    new_token = self.defs[name].token
                    # Expand the token
                    for old_p, new_p in zip(old_params, new_params):
                        new_token = re.sub(word_boundary(old_p), new_p, new_token)
                    # expanded_token = expanded_token.replace(_token.line, new_token)
                    new_token_val = self.try_eval_num(new_token)
                    new_token = str(new_token_val) if new_token_val else new_token
                    if _token.line == name:
                        expanded_token = re.sub(
                            word_boundary(_token.line), new_token, expanded_token
                        )
                    else:
                        expanded_token = expanded_token.replace(_token.line, new_token)
                    # Take care the remaining tokens
                    expanded_token = self.expand_token(
                        expanded_token, try_if_else, raise_key_error
                    )
                elif raise_key_error:
                    raise KeyError("token '{}' is not defined!".format(name))
                # else:
                #     expanded_token = expanded_token.replace(_token.line, '(0)')
            elif name is not expanded_token:
                params = self.expand_token(_token.line, try_if_else, raise_key_error)
                expanded_token = re.sub(
                    word_boundary(_token.line), params, expanded_token
                )
                # expanded_token = expanded_token.replace(match.group(0), self.expand_token(match.group(0)))

        if expanded_token in self.defs:
            expanded_token = self.expand_token(
                self.defs[token].token, try_if_else, raise_key_error
            )

            # try to eval the value, to reduce the bracket count
            token_val = self.try_eval_num(expanded_token)
            if token_val is not None:
                expanded_token = str(token_val)
        elif zero_undefined and len(tokens) and expanded_token == name:
            return "0"

        return expanded_token

    def get_expand_defines(self, filepath, try_if_else=True, ignore_header_guard=True):
        defines = []

        def expand_define(line, _):
            define = self._get_define(line)
            if define == None:
                return
            self.iterate = 0
            token = self.expand_token(define.token, try_if_else, raise_key_error=False)
            if define.name in self.defs:
                token_val = self.try_eval_num(token)
                if token_val is not None:
                    self.defs[define.name] = self.defs[define.name]._replace(
                        token=str(token_val)
                    )
            defines.append(
                DEFINE(
                    name=define.name,
                    params=define.params,
                    token=token,
                    line=line,
                )
            )

        with open(filepath, "r", errors="replace") as fs:
            self.read_file_lines(fs, expand_define, try_if_else, ignore_header_guard)
        return defines

    def get_expand_define(self, macro_name, try_if_else=True):
        if macro_name not in self.defs:
            return None

        define = self.defs[macro_name]
        token = define.token
        expanded_token = self.expand_token(token, try_if_else, raise_key_error=False)

        return DEFINE(
            name=macro_name,
            params=define.params,
            token=expanded_token,
            line=define.line,
        )

    def get_preprocess_source(self, filepath, try_if_else=True):
        lines = []

        def read_line(line, _):
            lines.append(line)

        ignore_header_guard = os.path.splitext(filepath)[1] == ".h"
        with open(filepath, "r", errors="replace") as fs:
            self.read_file_lines(
                fs,
                read_line,
                try_if_else,
                ignore_header_guard,
                reserve_whitespace=True,
            )
        return lines


CACHE_OBJ_FILE = os.path.join(sublime.cache_path(), "DimInavtiveCode.db")
PARSERS = {}
PARSER_IS_BUILDING = set()

REGION_INACTIVE_NAME = "inactive_source_code"
PREDEFINE_FOLDER = ".define_parser_predefine"
DEFAULT_SUPPORT_EXTS = ".c,.h,.cpp"

DP_SETTING_HL_INACTIVE = "define_parser_highlight_inactive_enable"
DP_SETTING_SUPPORT_EXT = "define_parser_highlight_extensions"
DP_SETTING_COMPILE_FILE = "define_parser_compile_flag_file"


def plugin_loaded():
    if os.path.exists(CACHE_OBJ_FILE):
        global PARSERS
        with open(CACHE_OBJ_FILE, "rb") as fs:
            PARSERS = pickle.load(fs)


def _get_folder(window):
    if window is None:
        return
    folders = window.folders()
    if len(folders) != 1:
        print("Currently only support one folder in a Window.")
        return None

    return folders[0]


def _init_parser(window):
    active_folder = _get_folder(window)
    if active_folder is None:
        return None

    print("init_parser", active_folder)
    PARSER_IS_BUILDING.add(active_folder)

    p = Parser()
    PARSERS[active_folder] = p

    predefines = _get_configs_from_file(
        window, window.settings().get(DP_SETTING_COMPILE_FILE)
    )
    for d in predefines:
        print(" insert: ", d)
        p.insert_define(d[0], token=d[1])

    def async_proc():
        PARSER_IS_BUILDING.remove(active_folder)
        p.read_folder_h(active_folder)
        sublime.status_message("building define database done.")
        if window.settings().get(DP_SETTING_HL_INACTIVE, True):
            _mark_inactive_code(window.active_view())
        with open(CACHE_OBJ_FILE, "wb") as fs:
            pickle.dump(PARSERS, fs)

    sublime.status_message("building define database, please wait...")
    sublime.set_timeout_async(async_proc, 0)


def _get_parser(window):
    active_folder = _get_folder(window)
    if active_folder not in PARSERS:
        return None
    return PARSERS[active_folder]


def _mark_inactive_code(view):
    window = view.window()
    if _get_folder(window) in PARSER_IS_BUILDING:
        return
    p = _get_parser(window)
    filename = view.file_name()
    if p is None or filename is None:
        return

    _, ext = os.path.splitext(filename)
    if ext not in window.settings().get(
        DP_SETTING_SUPPORT_EXT, DEFAULT_SUPPORT_EXTS
    ).split(","):
        return

    fileio = io.StringIO(view.substr(sublime.Region(0, view.size())))
    num_lines = len(fileio.readlines())
    inactive_lines = set(range(1, 1 + num_lines))

    def mark_inactive(line, lineno):
        inactive_lines.remove(lineno)

    fileio.seek(0)
    p.read_file_lines(
        fileio, mark_inactive, reserve_whitespace=True, ignore_header_guard=True
    )
    print("  inactive lines count: ", len(inactive_lines))

    regions = [
        sublime.Region(view.text_point(line - 1, 0), view.text_point(line, 0))
        for line in inactive_lines
    ]
    view.add_regions(
        REGION_INACTIVE_NAME,
        regions,
        scope="comment.block",
        flags=sublime.DRAW_NO_OUTLINE,
    )


def _unmark_inactive_code(view):
    print("unmark ", view.file_name())
    view.erase_regions(REGION_INACTIVE_NAME)


def _get_config_list(window):
    folder = _get_folder(window)
    if folder is None:
        return None

    config_path = os.path.join(folder, PREDEFINE_FOLDER)
    print(config_path)
    if os.path.exists(config_path):
        return glob_recursive(config_path, "")


def _get_configs_from_file(window, file_basename):
    folder = _get_folder(window)
    if folder is None or file_basename is None:
        return []
    select_config = os.path.join(folder, PREDEFINE_FOLDER, file_basename)

    insert_defs = []
    with open(select_config) as fs:
        compile_flags = " ".join(fs.readlines()).split(" ")
        for flag in compile_flags:
            flag = flag.strip()
            if flag.startswith("-D"):
                tokens = flag.replace("-D", "").split("=")
                if len(tokens) == 1:
                    insert_defname = tokens[0]
                    insert_value = "1"
                elif len(tokens) == 2:
                    insert_defname, insert_value = tokens
                else:
                    print("can not recognize %s" % flag)
                insert_defs.append((insert_defname, insert_value))

    return insert_defs


class BuildDefineDatabaseCommand(sublime_plugin.WindowCommand):
    def run(self):
        active_folder = _get_folder(self.window)
        if active_folder is None:
            return

        _init_parser(self.window)


class ToggleMarkInactiveCode(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        has_mark = self.window.settings().get(DP_SETTING_HL_INACTIVE, True)
        if has_mark:
            _unmark_inactive_code(view)
        else:
            _mark_inactive_code(view)
        self.window.settings().set(DP_SETTING_HL_INACTIVE, not has_mark)


class SelectConfiguration(sublime_plugin.WindowCommand):
    def run(self):
        folder = _get_folder(self.window)
        config_list = _get_config_list(self.window)
        if config_list is None:
            return
        config_path = os.path.join(folder, PREDEFINE_FOLDER)

        items = []
        selected_config = self.window.settings().get(DP_SETTING_COMPILE_FILE)
        print("current: ", selected_config)
        selected_index = -1
        for index, filename in enumerate(config_list):
            selected = filename == selected_config

            with open(filename) as fs:
                compile_flags = " ".join(fs.readlines())
                item = sublime.QuickPanelItem(os.path.relpath(filename, config_path))
                item.details = compile_flags
                item.kind = (
                    (sublime.KIND_ID_COLOR_GREENISH, "✓", "")
                    if selected
                    else (0, "", "")
                )
                items.append(item)

            if selected:
                selected_index = index

        self.window.show_quick_panel(
            items, on_select=self._on_select, selected_index=selected_index
        )

    def _on_select(self, selected_index):
        config_list = _get_config_list(self.window)
        if config_list is None:
            return
        config_file = config_list[selected_index]
        if config_file == self.window.settings().get(DP_SETTING_COMPILE_FILE):
            return
        self.window.settings().set(DP_SETTING_COMPILE_FILE, config_file)
        self.window.run_command("build_define_database")
        for view in self.window.views(include_transient=True):
            _unmark_inactive_code(view)


class AppendDefine(sublime_plugin.TextCommand):
    def run(self, edit, text):
        self.view.insert(edit, self.view.size(), text)


class ShowAllDefineCommand(sublime_plugin.WindowCommand):
    def run(self):
        folder = _get_folder(self.window)
        parser = _get_parser(self.window)
        if folder is None or parser is None:
            return
        if len(parser.defs) == 0:
            sublime.error_message("No #define found in " + folder)
            return
        new_view = self.window.new_file(sublime.TRANSIENT)
        new_view.set_name("Define Value - " + folder)
        new_view.set_syntax_file("Packages/C++/C.sublime-syntax")
        # texts = []

        def insert_defs():
            for define in parser.defs.values():
                token = define.token
                token_value = parser.try_eval_num(token)
                if token_value is not None:
                    line = "#define %-30s (0x%x)" % (define.name, token_value)
                else:
                    line = "#define %-30s (%s)" % (define.name, token)
                new_view.run_command("append_define", {"text": line + "\n"})
            sublime.status_message("%d defines found!" % len(parser.defs))

        sublime.set_timeout_async(insert_defs, 0)
        # new_view.run_command("insert", {"characters": "\n".join(texts)})


class CalcValue(sublime_plugin.TextCommand):
    def run(self, edit):
        window = sublime.active_window()
        view = window.active_view()

        parser = _get_parser(window)
        if parser is None:
            _init_parser(window)
            return

        region = view.sel()[0]
        if region.begin() == region.end():  # point
            region = view.word(region)

            # handle special line endings for Ruby
            language = view.settings().get("syntax")
            endings = view.substr(sublime.Region(region.end(), region.end() + 1))

            if "Ruby" in language and self.endings.match(endings):
                region = sublime.Region(region.begin(), region.end() + 1)
        symbol = view.substr(region)

        define = parser.get_expand_define(symbol)
        if define is not None:
            print(define)
            value = parser.try_eval_num(define.token)
            if value is not None:
                text = "{} ({})".format(value, hex(value))
            else:
                text = html.escape(convertall_dec2fmt(define.token))

            view.show_popup(
                "<em>Expansion of</em> <small>{}{}</small><br>{}".format(
                    define.name,
                    "(%s)" % (", ".join(define.params))
                    if define.params is not None
                    else "",
                    text,
                ),
                max_width=800,
            )
        else:
            expanded_token = parser.expand_token(symbol)
            view.show_popup(
                "<em>Expansion of</em> <small>{}</small><br>{}".format(
                    html.escape(symbol),
                    html.escape(convertall_dec2fmt(expanded_token, "0x{:02X}")),
                ),
                max_width=800,
            )


class EvtListener(sublime_plugin.EventListener):
    def on_new_window_async(self, window):
        active_folder = _get_folder(window)
        if active_folder is None:
            return
        _init_parser(window)

    def on_load_async(self, view):
        print("load", view.file_name())
        window = view.window()
        p = _get_parser(window)
        if p is None:
            _init_parser(window)

    # def on_reload(self, view):
    #     print("reload", view.file_name())

    def on_activated_async(self, view):
        print("activate", view.file_name())
        window = view.window()
        if window is None:
            return

        if window.settings().get(DP_SETTING_HL_INACTIVE, True):
            _mark_inactive_code(view)
        else:
            _unmark_inactive_code(view)


class VwListener(sublime_plugin.ViewEventListener):
    def on_load_async(self):
        window = sublime.active_window()
        p = _get_parser(window)
        if p is None:
            _init_parser(window)
