import logging
import os
import re
import subprocess

# import functools
from collections import OrderedDict, defaultdict, namedtuple
from pprint import pformat

DEFINE = namedtuple("DEFINE", ("name", "params", "token", "line", "file", "lineno"))
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

logger = logging.getLogger("Define Parser")


def glob_recursive(directory, ext=".c"):
    return [
        os.path.join(root, filename)
        for root, dirnames, filenames in os.walk(directory)
        for filename in filenames
        if filename.endswith(ext)
    ]


def is_git(folder):
    markers = {".git", ".gitlab"}
    files = set(os.listdir(folder))
    return len(markers & files)


def git_lsfiles(directory, ext=".h"):
    try:
        filelist = subprocess.check_output(
            ["git", "--git-dir", directory, "ls-files"],
            shell=True,  # remove flashing empty cmd window prompt
        )
    except subprocess.CalledProcessError:
        # fallback to normal glob if git command fail
        return glob_recursive(directory, ext)
    except FileNotFoundError:
        # fallback to normal glob if git command fail
        return glob_recursive(directory, ext)

    filelist = proc.stdout.decode().split("\n")
    return [
        os.path.join(directory, filename)
        for filename in filelist
        if filename.endswith(ext)
    ]


class DuplicatedIncludeError(Exception):
    """assert when parser can not found ONE valid include header file."""


class Parser:
    iterate = 0

    def __init__(self):
        self.reset()
        self.filelines = defaultdict(list)

    def reset(self):
        self.defs = OrderedDict()  # dict of DEFINE
        self.folder = ""

    def insert_define(self, name, *, params=None, token=None):
        """params: list of parameters required, token: define body"""
        new_params = params or []
        new_token = token or ""
        self.defs[name] = DEFINE(
            name=name,
            params=new_params,
            token=new_token,
            line="",
            file="",
            lineno=0,
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
            token = token.replace("!", " not ")
            return int(eval(token))
        except:
            return None

    def read_file_lines(
        self,
        fileio,
        try_if_else=True,
        ignore_header_guard=False,
        reserve_whitespace=False,
        include_block_comment=False,
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
                    if include_block_comment:
                        yield (line, line_no)
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
                    yield (line, line_no)
                continue
            single_line = re.sub(regex_line_break, "", multi_lines)
            if if_true_bmp == BIT(if_depth + 1) - 1:
                yield (single_line, line_no)
                if_done_bmp |= BIT(if_depth)
            elif try_if_else and (match_if or match_elif or match_else or match_endif):
                yield (single_line, line_no)
            multi_lines = ""

    def _get_define(self, line, filepath="", lineno=0):
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
        self.filelines[filepath].append(lineno)
        return DEFINE(
            name=name,
            params=param_list if parentheses else None,
            token=token,
            line=line,
            file=filepath,
            lineno=lineno,
        )

    def read_folder_h(self, directory, try_if_else=True):
        self.folder = directory

        if is_git(directory):
            header_files = git_lsfiles(directory, ".h")
        else:
            header_files = glob_recursive(directory, ".h")
        logger.debug("read_header cnt: %d", len(header_files))

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

            try:
                with open(filepath, "r", errors="replace") as fs:
                    for line, lineno in self.read_file_lines(fs, try_if_else):
                        match_include = re.match(REGEX_INCLUDE, line)
                        if match_include is not None:
                            # parse included file first
                            path = match_include.group("PATH")
                            included_file = get_included_file(path, src_file=filepath)
                            read_header(included_file)
                        define = self._get_define(line, filepath, lineno)
                        if define is None or define.name in pre_defined_keys:
                            continue
                        self.defs[define.name] = define

            except UnicodeDecodeError as e:
                logger.warning("Fail to open {!r}. {}".format(filepath, e))

            if filepath in header_files:
                header_done.add(filepath)

        for header_file in header_files:
            read_header(header_file)

        return True

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

        with open(filepath, "r", errors="replace") as fs:
            for line, lineno in self.read_file_lines(
                fs, try_if_else, ignore_header_guard
            ):
                define = self._get_define(line, filepath, lineno)
                if define == None:
                    continue
                self.iterate = 0
                token = self.expand_token(
                    define.token, try_if_else, raise_key_error=False
                )
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
                        file=filepath,
                        lineno=lineno,
                    )
                )
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
