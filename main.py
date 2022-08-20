import html
import io
import logging
import os
import pickle
import re

import sublime
import sublime_plugin

logger = logging.getLogger("define-parser")


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


CACHE_OBJ_FILE = os.path.join(sublime.cache_path(), "DimInavtiveCode.db")
PARSERS = {}
PARSER_IS_BUILDING = set()

REGION_INACTIVE_NAME = "inactive_source_code"
PREDEFINE_FOLDER = ".define_parser_compiler_files"

DP_SETTING_HL_INACTIVE = "highlight_inactive_enable"
DP_SETTING_SUPPORT_EXT = "highlight_inactive_extensions"
DP_SETTING_COMPILE_FILE = "compile_flag_file"
DP_SETTING_LOG_DEBUG = "define_parser_debug_log_enable"


def _get_default_settings():
    return sublime.load_settings("DefineParser.sublime-settings")


# special function for plugin loaded callback
def plugin_loaded():
    if _get_default_settings().get(DP_SETTING_LOG_DEBUG, False):
        logger.setLevel(logging.DEBUG)

    if not os.path.exists(CACHE_OBJ_FILE):
        return

    def _restore_cache():
        global PARSERS
        with open(CACHE_OBJ_FILE, "rb") as fs:
            PARSERS = pickle.load(fs)

    sublime.set_timeout_async(_restore_cache, 0)


def _get_setting(obj_has_settings, key, default=None):
    defaults = _get_default_settings()
    return obj_has_settings.settings().get(key, defaults.get(key, default))


def _set_setting(obj_has_settings, key, value):
    obj_has_settings.settings().set(key, value)


def _get_folder(window):
    if window is None:
        return
    folders = window.folders()
    if len(folders) != 1:
        logger.warning("Currently only support one folder in a Window.")
        return None

    # TODO: use root marks
    return folders[0]


def _init_parser(window):
    active_folder = _get_folder(window)
    if active_folder is None:
        return None

    logger.info("init_parser %s", active_folder)
    PARSER_IS_BUILDING.add(active_folder)

    p = Parser()
    PARSERS[active_folder] = p

    predefines = _get_configs_from_file(
        window, _get_setting(window, DP_SETTING_COMPILE_FILE)
    )
    for d in predefines:
        logger.debug("  predefine: %s", d)
        p.insert_define(d[0], token=d[1])

    def async_proc():
        p.read_folder_h(active_folder)
        PARSER_IS_BUILDING.remove(active_folder)

        if _get_setting(window, DP_SETTING_HL_INACTIVE):
            _mark_inactive_code(window.active_view())

        sublime.status_message("building define database done.")
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
    if ext not in _get_setting(window, DP_SETTING_SUPPORT_EXT).split(","):
        return

    fileio = io.StringIO(view.substr(sublime.Region(0, view.size())))
    num_lines = len(fileio.readlines())
    inactive_lines = set(range(1, 1 + num_lines))

    def _exection_code_cb(line, lineno):
        inactive_lines.remove(lineno)

    fileio.seek(0)
    p.read_file_lines(
        fileio,
        _exection_code_cb,
        reserve_whitespace=True,
        ignore_header_guard=True,
        block_comment_cb=_exection_code_cb,
    )
    logger.debug("inactive lines count: %d", len(inactive_lines))

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
    logger.debug("unmark %s", view.file_name())
    view.erase_regions(REGION_INACTIVE_NAME)


def _get_config_list(window):
    folder = _get_folder(window)
    if folder is None:
        return None

    config_path = os.path.join(folder, PREDEFINE_FOLDER)
    if os.path.exists(config_path):
        return glob_recursive(config_path, "")


def _get_configs_from_file(window, file_basename):
    folder = _get_folder(window)
    if folder is None or file_basename is None:
        return []
    select_config = os.path.join(folder, PREDEFINE_FOLDER, file_basename)
    if not os.path.isfile(select_config) or not os.path.exists(select_config):
        return []

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
                    logger.warning("can not recognize %s", flag)
                insert_defs.append((insert_defname, insert_value))

    return insert_defs


class RebuildDefineDatabaseCommand(sublime_plugin.WindowCommand):
    def run(self):
        active_folder = _get_folder(self.window)
        if active_folder is None:
            return

        _init_parser(self.window)


class ToggleMarkInactiveCode(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        has_mark = _get_setting(self.window, DP_SETTING_HL_INACTIVE)
        if has_mark:
            _unmark_inactive_code(view)
        else:
            _mark_inactive_code(view)
        _set_setting(self.window, DP_SETTING_HL_INACTIVE, not has_mark)


def _creat_new_compiler_flag_file(window, filename):
    with open(filename, "w") as fs:
        fs.write("-DTEST=1\n-DPLATFORM=WIN\n")
    window.open_file(filename)


class SelectConfiguration(sublime_plugin.WindowCommand):
    def run(self):
        folder = _get_folder(self.window)
        config_list = _get_config_list(self.window)
        config_path = os.path.join(folder, PREDEFINE_FOLDER)
        if config_list is None or len(config_list) == 0:
            if not sublime.ok_cancel_dialog(
                "No compiler flags file found! Create one and try again.",
                ok_title="Create For Me",
                title="Define Parser",
            ):
                return
            if not os.path.exists(config_path):
                os.makedirs(config_path)
            sublime.save_dialog(
                callback=lambda f: _creat_new_compiler_flag_file(self.window, f),
                directory=config_path,
                name="CONFIG_A",
            )
            return

        items = []
        selected_config = _get_setting(self.window, DP_SETTING_COMPILE_FILE)
        logger.info("current config: %s", selected_config)
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

        item = sublime.QuickPanelItem("Default (without compiler flags)")
        item.kind = (
            (sublime.KIND_ID_COLOR_GREENISH, "✓", "")
            if selected_index == -1
            else (0, "", "")
        )
        items.append(item)

        self.window.show_quick_panel(
            items, on_select=self._on_select, selected_index=selected_index
        )

    def _on_select(self, selected_index):
        config_list = _get_config_list(self.window)
        if config_list is None or len(config_list) == 0:
            return

        config_file = (
            config_list[selected_index] if selected_index < len(config_list) else ""
        )
        if config_file == _get_setting(self.window, DP_SETTING_COMPILE_FILE):
            return
        _set_setting(self.window, DP_SETTING_COMPILE_FILE, config_file)

        self.window.run_command("rebuild_define_database")
        for view in self.window.views(include_transient=True):
            _unmark_inactive_code(view)


class AppendDefine(sublime_plugin.TextCommand):
    def run(self, edit, text):
        self.view.insert(edit, self.view.size(), text)


class ShowAllDefinesCommand(sublime_plugin.WindowCommand):
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


class CalculateDefineValue(sublime_plugin.TextCommand):
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
            logger.debug(define)
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


class ToggleDefineParserDebugLog(sublime_plugin.WindowCommand):
    def run(self):
        show_debug = not _get_setting(self.window, DP_SETTING_LOG_DEBUG)
        if show_debug:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)
        _set_setting(self.window, DP_SETTING_LOG_DEBUG, show_debug)


class EvtListener(sublime_plugin.EventListener):
    def on_new_window_async(self, window):
        active_folder = _get_folder(window)
        if active_folder is None:
            return
        _init_parser(window)

    def on_load_async(self, view):
        logger.debug("load %s", view.file_name())
        window = view.window()

        if _get_setting(window, DP_SETTING_HL_INACTIVE):
            _mark_inactive_code(view)
        else:
            _unmark_inactive_code(view)

    def on_post_save_async(self, view):
        filename = view.file_name()
        logger.debug("save %s", filename)
        window = view.window()

        current_config = _get_setting(window, DP_SETTING_COMPILE_FILE)
        if filename == current_config:
            window.run_command("rebuild_define_database")
            for view in window.views(include_transient=True):
                _unmark_inactive_code(view)
            return

        if _get_setting(window, DP_SETTING_HL_INACTIVE):
            _mark_inactive_code(view)
        else:
            _unmark_inactive_code(view)

    def on_activated_async(self, view):
        window = view.window()
        filename = view.file_name()
        if window is None or filename is None:
            return
        logger.debug("activate %s", filename)

        if _get_setting(window, DP_SETTING_HL_INACTIVE):
            _mark_inactive_code(view)
        else:
            _unmark_inactive_code(view)


class VwListener(sublime_plugin.ViewEventListener):
    def on_load_async(self):
        window = sublime.active_window()
        p = _get_parser(window)
        if p is None:
            _init_parser(window)
