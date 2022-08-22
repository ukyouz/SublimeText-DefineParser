# SublimeText-DefineParser

![Hero Screenshot](images/hero-screenshot.png)

This is a python parser for calculating C #define values.

Support functions:

- Show cursor define value/ all define values
- Parse define values from the folder marked as root
- Mark inactive code under configurations

## Usages

Open a C project folder with sublime, this plugin will start building the define data.

With default settings, parser will lookup the closest parent folder that contains the following root markers.

```json
{
    "define_parser_root_markers": [".root", ".git", ".gitlab"],
}
```

After define data is built, you can enjoy the following features.

### Get the Define Value

By default, right clicking with `Alt` being pressed shows the calculated value of the define under current cursor position.

![Preview: Alt-Click Button1](images/preview-alt-click-button1.png)

Or, drag a selection with `Alt` being pressed.

![Preview: Alt-Drag Button1](images/preview-alt-drag-button1.png)

### Highlight Inactive Code Region

Inactive code region will be highlighted in gray by default.

![Preview: Highlight Inactive Code](images/preview-highlight-inactive.png)

If you don't this behavior at startup, change the follow setting:

```json
{
    "highlight_inactive_enable": true,
}
```

You can also run the `Define Parser: Toggle Highlight for Inactive Code` command to toggle the highlight state, or use the default keymap `Ctrl-\`.

The inactive code highlight by default only shown in the following extensions:

```json
{
    "highlight_inactive_extensions": [".h", ".c", ".cpp"],
}
```

If mismatch happened or the define data is corrupted, run the `Define Parser: Rebuild #define Data` command to rebuild.

## Compiler Configurations

For C compiler, some extra defines are specified in the compile command without being written in the source codes. To setup such extra defines, you can simply create a compiler flag file by running `Define Parser: Select Define Configuration` command. Follow the instructions, this plugin help you creating a config file in your root folder. After config file is created, you can choose the configuration you want to for more precise parsing result.

The file name will be used to regonized as the config name, and for the define parser usage, this plugin only take `-D` options.

After the config selection, it takes a while to rebuild the define data; then the new configuration takes affect and the inactive region changes accordingly.

For example, we specify the `-DENV=ENV_TEST` in our config file:

![Preview: Highlight Inactive Code with Config](images/preview-highlight-inactive-with-config.png)
