"""Provider configuration dialog.

Renders itself from the selected provider's ``config_fields`` — so a provider
that needs an API key, a token, or nothing at all all use the same dialog.
Values are stored in QgsSettings under wake/<provider id>/<field key>.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QLabel, QDialogButtonBox,
)
from qgis.core import QgsSettings


def _setting_key(provider_cls, field_key):
    return f"wake/{provider_cls.id}/{field_key}"


def load_settings(provider_cls):
    """Return the stored {field key: value} for a provider, falling back to each
    field's declared default when nothing has been saved yet."""
    settings = QgsSettings()
    return {
        f["key"]: settings.value(
            _setting_key(provider_cls, f["key"]), f.get("default", ""), type=str)
        for f in provider_cls.config_fields
    }


def is_configured(provider_cls):
    """True if the provider needs no config, or all its fields have values."""
    if not provider_cls.config_fields:
        return True
    values = load_settings(provider_cls)
    return all(values.get(f["key"]) for f in provider_cls.config_fields)


class ProviderConfigDialog(QDialog):
    def __init__(self, provider_cls, parent=None):
        super().__init__(parent)
        self.provider_cls = provider_cls
        self.setWindowTitle(f"Configure — {provider_cls.label}")
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)

        if provider_cls.help_text:
            help_lbl = QLabel(provider_cls.help_text)
            help_lbl.setWordWrap(True)
            help_lbl.setStyleSheet("color: gray;")
            help_lbl.setOpenExternalLinks(True)
            layout.addWidget(help_lbl)

        self._edits = {}
        if provider_cls.config_fields:
            form = QFormLayout()
            settings = QgsSettings()
            for field in provider_cls.config_fields:
                edit = QLineEdit()
                if field.get("secret"):
                    edit.setEchoMode(QLineEdit.EchoMode.Password)
                if field.get("placeholder"):
                    edit.setPlaceholderText(field["placeholder"])
                edit.setText(settings.value(
                    _setting_key(provider_cls, field["key"]),
                    field.get("default", ""), type=str))
                form.addRow(field["label"], edit)
                self._edits[field["key"]] = edit
            layout.addLayout(form)
        else:
            layout.addWidget(QLabel("This provider needs no configuration."))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save_and_accept(self):
        settings = QgsSettings()
        for key, edit in self._edits.items():
            settings.setValue(
                _setting_key(self.provider_cls, key), edit.text().strip())
        self.accept()
