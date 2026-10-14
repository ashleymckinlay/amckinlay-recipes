#!/usr/local/autopkg/python
"""See docstring for DownloadChangedRecorder class"""

from autopkglib import Processor

__all__ = ["DownloadChangedRecorder"]


class DownloadChangedRecorder(Processor):
    """Copies the boolean download_changed set by the previous URLDownloader into
    another variable, so a later download doesn't overwrite it. AutoPkg's %var%
    substitution only works with strings, so VariableSetter can't do this."""

    description = __doc__
    input_variables = {
        "result_output_var_name": {
            "required": True,
            "description": "Name of the variable to store download_changed in.",
        },
    }
    output_variables = {
        "result_output_var_name": {
            "description": "Variable named by result_output_var_name, set to the "
            "boolean value of download_changed.",
        },
    }

    def main(self) -> None:
        name = self.env["result_output_var_name"]
        self.env[name] = bool(self.env.get("download_changed", False))
        self.output(f"{name} = {self.env[name]}")


if __name__ == "__main__":
    PROCESSOR = DownloadChangedRecorder()
    PROCESSOR.execute_shell()
