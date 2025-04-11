import re

REGEX_SYNTAX_LINE_COMMENT = re.compile(r"(.*?)(//.*)")
REGEX_SYNTAX_INLINE_COMMENT = re.compile(r"(.*)(/\*.*\*/)(.*)")

def remove_comment(texts: list, keep_line_comment=False):

    def remove_oneline_comment(string) -> tuple:
        m = REGEX_SYNTAX_INLINE_COMMENT.match(string)
        if m:
            return remove_oneline_comment(m.group(1) + (' ' * len(m.group(2))) + m.group(3))

        m = re.match(r'(.*)(/\*.*)', string)
        if m:
            return (m.group(1), True)

        m = REGEX_SYNTAX_LINE_COMMENT.match(string)
        if m:
            if keep_line_comment:
                return (string, False)
            else:
                return (m.group(1), False)

        return (string, False)

    def remove_block_comment_end(string) -> tuple:
        m = re.match(r'(.*\*/)(.*)', string)
        if m:
            text_ret = (' ' * len(m.group(1))) + m.group(2)
            return text_ret, False
        else:
            return ('', True)

    multi_comment = False
    for line in texts:
        if multi_comment is False:
            clean_txt, multi_comment = remove_oneline_comment(line)
            yield clean_txt
        else:
            clean_txt, multi_comment = remove_block_comment_end(line)
            if multi_comment is False:
                clean_txt, multi_comment = remove_oneline_comment(clean_txt)
            yield clean_txt


REG_LITERALS = [
    re.compile(r"\b(?P<NUM>[0-9]+)(?:##)?([ul]|ull?|ll?u|ll)\b", re.IGNORECASE),
    re.compile(r"\b(?P<NUM>0b[01]+)(?:##)?([ul]|ull?|ll?u|ll)\b", re.IGNORECASE),
    re.compile(r"\b(?P<NUM>0[0-7]+)(?:##)?([ul]|ull?|ll?u|ll)\b", re.IGNORECASE),
    re.compile(r"\b(?P<NUM>0x[0-9a-f]+)(?:##)?([ul]|ull?|ll?u|ll)\b", re.IGNORECASE),
]
REG_SPECIAL_SIZEOFTYPES = [
    re.compile(r"sizeof\(\s*U8\s*\)"),
    re.compile(r"sizeof\(\s*U16\s*\)"),
    re.compile(r"sizeof\(\s*U32\s*\)"),
    re.compile(r"sizeof\(\s*U64\s*\)"),
]
REG_SPECIAL_TYPES = [
    re.compile(r"\(\s*U8\s*\)"),
    re.compile(r"\(\s*U16\s*\)"),
    re.compile(r"\(\s*U32\s*\)"),
    re.compile(r"\(\s*U64\s*\)"),
]
REGEX_OPERATOR_NOT = re.compile(r"!([^=])")
REGEX_CHAR = re.compile(r"'([ -~])'")

def convert_op_c2py(txt: str) -> str:
    # remove integer literals type hint
    for re_reg in REG_LITERALS:
        txt = re_reg.sub(r"\1", txt)
    # calculate size of special type
    # transform type cascading to bit mask for equivalence calculation
    for data_sz, reg_sizeof_type, reg_special_type in zip(
        [1, 2, 4, 8], REG_SPECIAL_SIZEOFTYPES, REG_SPECIAL_TYPES
    ):
        # limitation:
        #   for equation like (U32)1 << (U32)(15) may be calculated to wrong value
        #   due to operator order
        # sizeof(U16) -> 2
        txt = reg_sizeof_type.sub(str(data_sz), txt)
        # (U16)x -> 0xFFFF & x
        txt = reg_special_type.sub("0x%s & " % ("F" * data_sz * 2), txt)
    # syntax translation from C -> Python
    txt = txt.replace("/", "//")
    txt = txt.replace("&&", " and ")
    txt = txt.replace("||", " or ")
    txt = REGEX_OPERATOR_NOT.sub(r" not \1", txt)
    for char in REGEX_CHAR.finditer(txt):
        txt = txt.replace(char.group(), str(ord(char[1])))
    return txt


def get_token_param_str(params) -> str:
    """return '(xx, xx, xx, ...)' """
    if len(params) and params[0] != "(":
        return ""
    # (() ())
    brackets = 0
    new_params = ""
    for c in params:
        brackets += (c == "(") * 1 + (c == ")") * -1
        new_params += c
        if brackets == 0:
            break
    return new_params


def _has_paired_parentheses(txt: str) -> bool:
    lparan_cnt = 0
    rparan_cnt = 0
    for char in txt:
        if char == "(":
            lparan_cnt += 1
        if char == ")":
            rparan_cnt += 1
    return lparan_cnt == rparan_cnt


def iter_arguments(params):
    if len(params) == 0:
        return []
    assert params[0] == "(" and params[-1] == ")", "`params` shall be like '(...)'"
    parma_list = params[1:-1].split(",")
    arguments = []
    for arg in parma_list:
        arguments.append(arg.strip())
        param_str = ",".join(arguments)
        if param_str and _has_paired_parentheses(param_str):
            yield param_str
            arguments = []