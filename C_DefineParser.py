import logging
import os
import re
import subprocess
from pathlib import Path

# import functools
from collections import Counter, defaultdict, namedtuple
from contextlib import contextmanager
from pprint import pformat

from .utils.txt_op import remove_comment, convert_op_c2py, get_token_param_str, iter_arguments


Define = namedtuple(
    "Define",
    ("name", "params", "token", "line", "file", "lineno"),
)
Token = namedtuple("Token", ("name", "params", "line", "span"))

WORD_BOUNDARY = lambda word: r"\b(\s*##\s*)?%s\b" % re.escape(word)

REGEX_TOKEN = re.compile(r"\b(?P<NAME>[a-zA-Z_][a-zA-Z0-9_]+)\b")
REGEX_DEFINE = re.compile(
    r"#\s*define\s+"
    + REGEX_TOKEN.pattern
    + r"(?P<HAS_PAREN>\((?P<PARAMS>[\w\., ]*)\))*\s*(?P<TOKEN>.+)*"
)
REGEX_UNDEF = re.compile(r"#\s*undef\s+" + REGEX_TOKEN.pattern)
REGEX_INCLUDE = re.compile(r'#\s*include\s+["<](?P<PATH>.+)[">]\s*')
REGEX_STRING = re.compile(r'"[^"]+"')

logger = logging.getLogger("Define Parser")


def glob_recursive(directory, exts=None):
    exts = exts or [".h", ".H"]
    logger.debug("glob **/*.{%s} --recursieve", exts)
    files = set()
    for ext in exts:
        files |= set(Path(directory).rglob("*.%s" % ext))
    return list(files)


def is_git(folder):
    markers = {".git", ".gitlab"}
    files = set(os.listdir(folder))
    return len(markers & files)


def git_lsfiles(directory, exts=None, recurse_submodule=False):
    exts = exts or [".h"]
    git_cmds = ["git", "--git-dir", os.path.join(directory, ".git"), "ls-files"]
    if recurse_submodule:
        git_cmds.append("--recurse-submodules")
    logger.debug(" ".join(git_cmds))
    try:
        filelist_output = subprocess.check_output(
            git_cmds,
            shell=True,  # remove flashing empty cmd window prompt
        )
    except subprocess.CalledProcessError:
        # fallback to normal glob if git command fail
        return glob_recursive(directory, exts)
    except FileNotFoundError:
        # fallback to normal glob if git command fail
        return glob_recursive(directory, exts)

    folder = Path(directory)
    filelist = filelist_output.decode().split("\n")
    filelist = [folder / filename for filename in filelist]
    return [f for f in filelist if f.suffix in exts]



REG_STATEMENT_IF = re.compile(r"\s*#\s*if(\s+|\b)(?P<TOKEN>.+)")
REG_STATEMENT_IFDEF = re.compile(r"\s*#\s*ifdef(\s+|\b)(?P<TOKEN>.+)")
REG_STATEMENT_IFNDEF = re.compile(r"\s*#\s*ifndef(\s+|\b)(?P<TOKEN>.+)")
REG_STATEMENT_ELIF = re.compile(r"\s*#\s*elif(\s+|\b)(?P<TOKEN>.+)")
REG_STATEMENT_ELSE = re.compile(r"\s*#\s*else")
REG_STATEMENT_ENDIF = re.compile(r"\s*#\s*endif")


REGEX_SYNTAX_LINE_BREAK = re.compile(r"\\\s*$")

REGEX_MACRO_HASH_OP = re.compile(r"\s*#\s*(?P<ARG>[^\s]+)")
REGEX_MACRO_VA_ARGS = re.compile(r"(?:(,)\s*##\s*)?__VA_ARGS__")


class DuplicatedIncludeError(Exception):
    """assert when parser can not found ONE valid include header file."""


IncludeHeader = namedtuple("IncludeHeader", ["inc_path", "src_file"])

class CodeActiveState:
    """active state of a code region inside #if/... directives"""

    def __init__(self, condition):
        self._active = bool(condition)

    def __bool__(self) -> bool:
        return self._active

    def meet_elif(self, condition):
        if self._active:
            self._active = False
        else:
            self._active = bool(condition)

    def meet_else(self):
        self._active = not self._active


class CDefineEnv:
    def __init__(self):
        self._globals = {}  # use for eval

    def add_expr(self, code):
        try:
            exec(code, self._globals)
        except NameError:
            pass
        except SyntaxError:
            pass
        except TypeError:
            pass

    def add_define(self, define: Define):
        if define.params is None:
            code = "%s = %s" % (define.name, convert_op_c2py(define.token))
            self.add_expr(code)
        else:
            code = "def %s(%s): return %s" % (define.name, ",".join(define.params), define.token)
            self.add_expr(code)

    def del_name(self, name):
        try:
            exec("del %s" % name, self._globals)
        except NameError:
            pass

    def try_eval_num(self, token):
        token = convert_op_c2py(token)
        try:
            return int(eval(token, self._globals))
        except:
            return None

    def stringify_token(self, line: str, old_params: list = None) -> str:
        expanded_token = line
        for mark_match in REGEX_MACRO_HASH_OP.finditer(line):
            # ie: #define stringify(var)    #var
            arg = mark_match.group("ARG")
            if old_params and arg not in old_params:
                raise SyntaxError("'#' is not followed by a macro parameter, got: %r" % arg)
            expanded_token = mark_match.re.sub('"%s"' % arg, expanded_token)

        return expanded_token


def has_defined(define: Define, curr_file, curr_line):
    defined_file = define.file
    defined_line = define.lineno
    if (
        defined_file
        and os.path.samefile(defined_file, curr_file)
        and curr_line < defined_line
    ):
        return False
    else:
        return True


def _arguments_expansion(cdef: CDefineEnv, define: Define, t: Token, check=False) -> str:
    old_params = define.params or []
    new_params = list(iter_arguments(t.params or ""))
    variadic_pos = old_params.index("...") if "..." in old_params else -1

    if check and variadic_pos == -1 and len(old_params) != len(new_params):
        raise SyntaxError(
            "macro {!r} requires {} arguments, but {} given: {!r}".format(
                t.name, len(old_params), len(new_params), t.params
            )
        )

    if variadic_pos >= 0:
        if len(old_params) - 1 <= len(new_params):
            old_params = old_params[:variadic_pos]
        else:
            raise SyntaxError(
                "macro {!r} requires at least {} arguments, but {} given: {!r}".format(
                    t.name, len(old_params) - 1, len(new_params), t.params
                )
            )

    new_token = define.token
    old_param_regs = (re.compile(WORD_BOUNDARY(x)) for x in old_params)
    for old_p_reg, new_p in zip(old_param_regs, new_params):
        new_token = old_p_reg.sub(new_p, new_token)

    if variadic_pos >= 0 and len(new_params) >= variadic_pos:
        if REGEX_MACRO_VA_ARGS.search(new_token):
            """
            #define LOG(msg, ...)  printf(msg, __VA_ARGS__)
            #define LOG2(msg, ...)  printf(msg, ## __VA_ARGS__)

            LOG("xx")        ->  printf("xx", )
            LOG("xx", 123)   ->  printf("xx", 123)
            LOG2("xx")       ->  printf("xx")
            LOG2("xx", 123)  ->  printf("xx", 123)
            """
            new_param_txt = ",".join(new_params[variadic_pos:])

            new_token = REGEX_MACRO_VA_ARGS.sub(
                (r"\g<1>" if new_param_txt else "") + new_param_txt,
                new_token,
            )

    new_token = re.sub(r"\s*##\s*", "", new_token)
    new_token_val = cdef.try_eval_num(new_token)
    if new_token_val is not None:
        return str(new_token_val)
    else:
        return new_token


def _argument_replacement(t: Token, new_token: str, line: str):
    # Replace original line with new parameterized-token
    expanded_token = line
    if t.line == t.name:
        expanded_token = re.sub(WORD_BOUNDARY(t.line), new_token, expanded_token)
    else:
        expanded_token = expanded_token.replace(t.line, new_token)

    return expanded_token


def _search_included_file(header_files: list, inc_path, src_file):
    inc_path = os.path.normpath(inc_path)  # xxx/conf.h
    src_file = os.path.normpath(src_file)  # C:/path/to/src.xxx.c
    included_files = [
        h
        for h in header_files
        if inc_path in h and os.path.basename(inc_path) == os.path.basename(h)
    ]
    if len(included_files) > 1:
        included_files = [f for f in included_files if f.replace(inc_path, "") in src_file]

    if len(included_files) == 0:
        return None

    relativities = [(len(os.path.commonpath([f, src_file])), f) for f in included_files]
    relativities.sort(key=lambda x: x[0], reverse=True)
    counter = Counter([x[0] for x in relativities])
    if counter[relativities[0][0]] > 1:
        raise DuplicatedIncludeError(pformat(included_files, indent=4, width=120))

    if len(included_files):
        return included_files[0]
    else:
        return None


class Parser:
    def __init__(self):
        self.reset()
        self.filelines = defaultdict(list)

    def reset(self):
        self.cdef = CDefineEnv()
        self.defs = {}  # dict of Define
        self.zero_defs = set()
        self.folder = ""
        self.include_trees = defaultdict(list)  # dict[filename: str, include_files: list[str]]
        self.header_files = []
        self.temp_defs = defaultdict(set)
        self.recurse_submodule = False

    def insert_define(self, name, *, params=None, token=None, filename="", lineno=0):
        """params: list of parameters required, token: define body"""
        new_params = params or []
        new_token = token or ""
        define = Define(
            name=name,
            params=new_params,
            token=new_token,
            line="",
            file=filename,
            lineno=lineno,
        )
        self._insert_define(define)

    def _insert_define(self, define: Define):
        self.defs[define.name] = define
        self.cdef.add_define(define)

    def remove_define(self, name):
        if name in self.defs:
            del self.defs[name]
            self.cdef.del_name(name)
        elif name in self.zero_defs:
            self.zero_defs.remove(name)
            self.cdef.del_name(name)
        else:
            raise KeyError("token '{}' is not defined!".format(name))

    def insert_temp_define(self, name, *, params=None, token=None, filename="", lineno=0):
        logger.debug("insert temp define: %s", name)
        self.temp_defs[filename].add(name)
        self.insert_define(name, params=params, token=token, filename=filename, lineno=lineno)

    def remove_temp_define(self, filename):
        logger.debug("remove %d temp defines", len(self.temp_defs[filename]))
        for name in self.temp_defs[filename]:
            if name in self.defs:
                del self.defs[name]
                self.cdef.del_name(name)
        self.temp_defs[filename] = set()

    def read_file_lines(
        self,
        fileio,
        try_if_else=True,
        ignore_header_guard=False,
        reserve_whitespace=False,
    ):
        captured_ifs = []
        def is_active(single_line: str = "") -> bool:
            match_if = REG_STATEMENT_IF.match(single_line)
            match_ifdef = REG_STATEMENT_IFDEF.match(single_line)
            match_ifndef = REG_STATEMENT_IFNDEF.match(single_line)
            match_elif = REG_STATEMENT_ELIF.match(single_line)
            match_else = REG_STATEMENT_ELSE.match(single_line)
            match_endif = REG_STATEMENT_ENDIF.match(single_line)
            top_visible_level = all(bool(active) for active in captured_ifs)
            if match_if:
                if_token_val = self.expand_token(match_if.group("TOKEN"))
                captured_ifs.append(CodeActiveState(self.cdef.try_eval_num(if_token_val)))
            elif match_ifdef:
                check_name = match_ifdef.group("TOKEN").rstrip()
                if check_name in self.defs:
                    has_def = has_defined(self.defs[check_name], fileio.name, line_no)
                else:
                    has_def = False
                captured_ifs.append(CodeActiveState(has_def))
            elif match_ifndef:
                if ignore_header_guard and captured_ifs == []:
                    captured_ifs.append(CodeActiveState(True))
                else:
                    if fileio.name.endswith(".h") and captured_ifs == []:
                        has_def = False
                    else:
                        check_name = match_ifndef.group("TOKEN").rstrip()
                        if check_name in self.defs:
                            has_def = has_defined(self.defs[check_name], fileio.name, line_no)
                        else:
                            has_def = False
                    captured_ifs.append(CodeActiveState(not has_def))
            elif match_elif:
                if_token_val = self.expand_token(match_elif.group("TOKEN"))
                captured_ifs[-1].meet_elif(self.cdef.try_eval_num(if_token_val))
            elif match_else:
                captured_ifs[-1].meet_else()
            elif match_endif:
                if captured_ifs:
                    captured_ifs.pop()
                else:
                    # some source files may tend to leave an extra #endif at the end
                    # I think it is for unintentionally include, so just warn and let it go.
                    logger.warning("Extra #endif found in {}#{}".format(fileio.name, line_no))
                    return False
            return top_visible_level or all(bool(active) for active in captured_ifs)

        merged_line = ""
        clean_code = remove_comment(fileio.readlines())
        for line_no, line in enumerate(clean_code, 1):

            merged_line += REGEX_SYNTAX_LINE_BREAK.sub(" ", line.strip())
            if REGEX_SYNTAX_LINE_BREAK.search(line):
                if reserve_whitespace:
                    if is_active():
                        yield (line, line_no)
                continue

            if not try_if_else or is_active(merged_line):
                yield (merged_line, line_no)

            merged_line = ""

    def _do_define_directive(self, line, filepath="", lineno=0):
        match = REGEX_UNDEF.match(line)
        if match is not None:
            name = match.group("NAME")
            if name in self.defs:
                del self.defs[name]
                self.cdef.del_name(name)
            return

        match = REGEX_DEFINE.match(line)
        if match is None:
            return

        name = match.group("NAME")
        parentheses = match.group("HAS_PAREN")
        params = match.group("PARAMS")
        param_list = [p.strip() for p in params.split(",")] if params else []
        match_token = match.group("TOKEN") or ""
        token = match_token.strip() or "(1)"

        """
        #define AAA     // params = None
        #define BBB()   // params = []
        #define CCC(a)  // params = ['a']
        """
        self.filelines[filepath].append(lineno)
        return Define(
            name=name,
            params=param_list if parentheses else None,
            token=token,
            line=line,
            file=filepath,
            lineno=lineno,
        )

    def read_folder_h(self, directory, try_if_else=True, exts=None):
        exts = exts or [".h"]
        self.folder = directory

        if is_git(directory):
            header_files = git_lsfiles(directory, exts, self.recurse_submodule)
        else:
            header_files = glob_recursive(directory, exts)
        self.header_files = [os.path.normpath(f) for f in header_files]
        logger.debug("read_header cnt: %d", len(header_files))

        header_done = set()
        pre_defined_keys = self.defs.keys()

        def read_header(filepath):
            if filepath is None or filepath in header_done:
                return
            header_done.add(filepath)

            try:
                with open(filepath, "r", errors="replace") as fs:
                    for line, lineno in self.read_file_lines(fs, try_if_else):
                        match_include = REGEX_INCLUDE.match(line)
                        if match_include is not None:
                            # parse included file first
                            path = match_include.group("PATH")
                            included_file = _search_included_file(
                                self.header_files, path, src_file=filepath
                            )
                            if included_file:
                                self.include_trees[os.path.realpath(filepath)].append(
                                    IncludeHeader(path, os.path.realpath(included_file))
                                )
                                read_header(included_file)
                        define = self._do_define_directive(line, filepath, lineno)
                        if define is None or define.name in pre_defined_keys:
                            continue
                        self._insert_define(define)

            except UnicodeDecodeError as e:
                logger.warning("Fail to open {!r}. {}".format(filepath, e))

        for header_file in header_files:
            read_header(header_file)

        return True

    @contextmanager
    def read_h(self, filepath, try_if_else=False):
        try:
            with open(filepath, "r", errors="replace") as fs:
                for line, _ in self.read_file_lines(fs, try_if_else):
                    define = self._do_define_directive(line)
                    if define is None:
                        continue
                    # if len(define.params):
                    #     return
                    self._insert_define(define)
            yield
        except UnicodeDecodeError as e:
            print("Fail to open :{}. {}".format(filepath, e))

    @contextmanager
    def read_c(self, filepath, try_if_else=False):
        """use `with` context manager for having temporary tokens defined in .c source file"""
        temp_defs = []
        temp_hidden = []
        try:
            add_includes = os.path.realpath(filepath) not in self.include_trees
            with open(filepath, "r", errors="replace") as fs:
                for line, line_no in self.read_file_lines(fs, try_if_else):
                    if add_includes:
                        match_include = REGEX_INCLUDE.match(line)
                        if match_include is not None:
                            path = match_include.group("PATH")
                            included_file = _search_included_file(
                                self.header_files, path, src_file=filepath
                            )
                            if included_file:
                                self.include_trees[os.path.realpath(filepath)].append(
                                    IncludeHeader(path, os.path.realpath(included_file))
                                )
                            continue
                    define = self._do_define_directive(line, filepath, line_no)
                    if define is None:
                        continue
                    # if len(define.params):
                    #     return
                    if define.name in self.defs:
                        temp_hidden.append(self.defs[define.name])
                    temp_defs.append(define)

            for define in temp_defs:
                self._insert_define(define)

            yield

        except UnicodeDecodeError as e:
            print("Fail to open :{}. {}".format(filepath, e))
        finally:
            for define in temp_defs:
                del self.defs[define.name]
                self.cdef.del_name(define.name)
            # restore temp hidden
            for define in temp_hidden:
                self._insert_define(define)

    def load_compile_flags(self, compile_flag_txt: str=""):
        if compile_flag_txt == "":
            return

        compile_flags = " ".join(compile_flag_txt.splitlines()).split(" ")

        predefines = []
        for arg in compile_flags:
            if not arg.startswith("-D"):
                continue

            pair = tuple(arg[2:].split("="))
            if len(pair) == 1:
                # ie: -DDEBUG
                predefines.append((pair[0], 0))
            elif len(pair) == 2:
                # ie: -DDEBUG=0
                predefines.append(pair)

        for d in predefines:
            print("  predefine: {!r}".format(d))
            self.insert_define(d[0], token=d[1])

    def find_tokens(self, token) -> list:

        # remove string value in token
        string_spans = [m.span() for m in REGEX_STRING.finditer(token)]

        tokens = list(REGEX_TOKEN.finditer(token))
        if len(tokens):
            ret_tokens = []
            for match in tokens:
                if any(s[0] < match.start() and match.end() < s[1] for s in string_spans):
                    # skip tokens in string
                    continue
                _token = match.group("NAME")
                params = None
                if _token in self.defs and self.defs[_token].params is not None:
                    params = get_token_param_str(token[match.end() :])
                elif match.end() < len(token) and token[match.end()] == "(":
                    # to suppress error message:
                    # <string>:1: SyntaxWarning: 'int' object is not callable; perhaps you missed a comma?
                    params = get_token_param_str(token[match.end() :])
                param_str = params if params else ""
                ret_tokens.append(
                    Token(
                        name=_token, params=params, line=_token + param_str, span=match.span()
                    )
                )
            return ret_tokens
        else:
            return []

    def expand_token(self, token: str, zero_undefined=False):

        token_val = self.cdef.try_eval_num(token)
        if token_val is not None:
            return str(token_val)

        total_seen = set()

        def _expand_token(_token: str, avoid_recursion_set: set):
            expanded_token = _token.strip()
            simple_tokens = [t for t in self.find_tokens(expanded_token) if not t.params]
            """
            token    token    token
            __       __       _
            ALIGN_2N(XX_BASE, 4)
            """
            token_seen = avoid_recursion_set.copy()
            for _t in simple_tokens:
                total_seen.add(_t.name)

                if _t.name not in token_seen and _t.name in self.defs:
                    define = self.defs[_t.name]
                    if not define.params:
                        # TODO: shall check `if define.params is not None`
                        # but hang in unittest, don't know why
                        new_token = _arguments_expansion(self.cdef, define, _t, False)
                        token_seen.add(_t.name)
                        new_token = _expand_token(new_token, token_seen)
                        token_seen.remove(_t.name)

                        expanded_token = _argument_replacement(
                            _t, new_token, expanded_token
                        )
                elif _t.name in self.zero_defs:
                    expanded_token = re.sub(WORD_BOUNDARY(_t.name), "0", expanded_token)
                elif _t.line == _t.name and zero_undefined:
                    self.zero_defs.add(_t.name)
                    self.cdef.add_expr("%s = 0" % _t.name)
                    expanded_token = re.sub(WORD_BOUNDARY(_t.name), "0", expanded_token)

            parameterized_tokens = [t for t in self.find_tokens(expanded_token) if t.params]
            for _t in parameterized_tokens:
                total_seen.add(_t.name)
                if _t.name not in self.defs:
                    continue
                if _t.name in token_seen:
                    continue

                define = self.defs[_t.name]
                if "#" in define.token:
                    new_token = _arguments_expansion(self.cdef, define, _t, False)
                    new_token = self.cdef.stringify_token(new_token)
                    expanded_token = _argument_replacement(_t, new_token, expanded_token)
                else:
                    new_token = _arguments_expansion(self.cdef, define, _t, True)
                    token_seen.add(_t.name)
                    new_token = _expand_token(new_token, token_seen)
                    token_seen.remove(_t.name)
                    expanded_token = _argument_replacement(_t, new_token, expanded_token)

            if _token != expanded_token:
                new_tokens = set(t.name for t in self.find_tokens(expanded_token))
                new_tokens ^= total_seen
                if len(new_tokens):
                    expanded_token = _expand_token(expanded_token, token_seen)

            # token_val = self.cdef.try_eval_num(expanded_token)
            # if token_val is not None:
            #     return str(token_val)

            return expanded_token

        return _expand_token(token, total_seen)

    def get_expand_defines(
        self, filepath, try_if_else=True, ignore_header_guard=True
    ) -> list:
        defines = []

        with open(filepath, "r", errors="replace") as fs:
            for line, lineno in self.read_file_lines(fs, try_if_else, ignore_header_guard):
                define = self._do_define_directive(line, filepath, lineno)
                if define is None:
                    continue
                if define.params is None:
                    expanded_token = self.expand_token(define.token)
                else:
                    expanded_token = define.token
                defines.append(
                    Define(
                        name=define.name,
                        params=define.params,
                        token=expanded_token,
                        line=line,
                        file=filepath,
                        lineno=lineno,
                    )
                )
        return defines

    def get_expand_define(self, macro_name):
        if macro_name not in self.defs:
            return None

        define = self.defs[macro_name]
        token = define.token
        expanded_token = self.expand_token(token)

        return Define(
            name=macro_name,
            params=define.params,
            token=expanded_token,
            line=define.line,
            file=define.file,
            lineno=define.lineno,
        )

    def get_preprocess_source(self, filepath, try_if_else=True):
        lines = []

        ignore_header_guard = os.path.splitext(filepath)[1] == ".h"
        with open(filepath, "r", errors="replace") as fs:
            for line, _ in self.read_file_lines(
                fs,
                try_if_else,
                ignore_header_guard,
                reserve_whitespace=True,
            ):
                lines.append(line)
        return lines

    @contextmanager
    def pickable(self):
        cdef_backup = self.cdef
        self.cdef = CDefineEnv()
        yield self
        self.cdef = cdef_backup