# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from anki.import_export_pb2 import CampaignNotetypeSelection
from aqt.qt import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    Qt,
    QVBoxLayout,
    QWidget,
    qconnect,
)

if TYPE_CHECKING:
    from anki.collection import Collection
    from aqt.main import AnkiQt

SHARING = "donationCampaignSharing"


class NotetypeChoice(TypedDict):
    include: bool
    flds: list[str]
    tmpls: list[str]


Sharing = dict[str, NotetypeChoice]


def _key(item: dict[str, Any]) -> str:
    if item.get("id") is not None:
        return f"id:{item['id']}:{item['name']}"
    return f"name:{item['name']}"


def _all_keys(model: dict[str, Any], group: str) -> list[str]:
    return [_key(item) for item in model[group]]


def current_choices(
    models: list[dict[str, Any]], saved: dict[str, dict[str, Any]] | None
) -> Sharing:
    # New note types, fields and templates start unchecked once the user has
    # saved a selection. Saved types without "include" predate that key and
    # were shared, so they stay included.
    choices: Sharing = {}
    for model in models:
        mid = str(model["id"])
        if saved is None:
            choices[mid] = {
                "include": True,
                "flds": _all_keys(model, "flds"),
                "tmpls": _all_keys(model, "tmpls"),
            }
            continue
        remembered = saved.get(mid)
        if remembered is None:
            choices[mid] = {"include": False, "flds": [], "tmpls": []}
            continue
        flds, tmpls = (
            [key for key in _all_keys(model, group) if key in remembered.get(group, [])]
            for group in ("flds", "tmpls")
        )
        choices[mid] = {
            "include": bool(remembered.get("include", True)),
            "flds": flds,
            "tmpls": tmpls,
        }
    return choices


def describe_sharing(col: Collection, saved: dict[str, dict[str, Any]] | None) -> str:
    """Summarize how much note text the saved choices share."""
    models = col.models.all()
    choices = current_choices(models, saved)
    included = [m for m in models if choices[str(m["id"])]["include"]]
    selected = sum(len(choices[str(m["id"])]["flds"]) for m in included)
    total = sum(len(m["flds"]) for m in included)
    if not selected:
        return col.tr.qt_misc_campaign_sharing_nothing()
    if selected == total and len(included) == len(models):
        return col.tr.qt_misc_campaign_sharing_all(notetype_count=len(models))
    return col.tr.qt_misc_campaign_sharing_some(
        selected=selected,
        total=total,
        included_count=len(included),
        notetype_count=len(models),
    )


def export_campaign(
    col: Collection, path: str, choices: Sharing, collection_id: str
) -> None:
    # Excluded note types get no selection, so the backend blanks all their
    # fields and templates.
    selections = [
        CampaignNotetypeSelection(
            notetype_id=int(mid), fields=choice["flds"], templates=choice["tmpls"]
        )
        for mid, choice in choices.items()
        if choice["include"]
    ]
    col.export_campaign_package(
        out_path=path, selections=selections, collection_id=collection_id
    )


class SharingDialog(QDialog):
    def __init__(self, mw: AnkiQt, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.mw = mw
        self.profile = mw.pm.profile
        self.trs = mw.col.tr
        self.models = sorted(mw.col.models.all(), key=lambda m: m["name"].casefold())
        self.choices = current_choices(self.models, self.profile.get(SHARING))
        self.setWindowTitle(self.trs.qt_misc_campaign_preferences())
        self.resize(760, 540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(14)
        columns = QHBoxLayout()
        columns.setSpacing(18)
        layout.addLayout(columns, 1)
        sidebar = QVBoxLayout()
        columns.addLayout(sidebar, 1)
        sidebar.setSpacing(8)
        sidebar.addWidget(QLabel(self.trs.qt_misc_campaign_note_types()))
        self.types = QListWidget()
        self.types.setAccessibleName(self.trs.qt_misc_campaign_note_types())
        for model in self.models:
            entry = QListWidgetItem(model["name"])
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(self._state(self.choices[str(model["id"])]["include"]))
            self.types.addItem(entry)
        sidebar.addWidget(self.types)
        type_buttons = QHBoxLayout()
        sidebar.addLayout(type_buttons)
        self.include_types = self._button(
            self.trs.qt_misc_campaign_include_all(),
            self.trs.qt_misc_campaign_note_types(),
            type_buttons,
        )
        self.exclude_types = self._button(
            self.trs.qt_misc_campaign_exclude_all(),
            self.trs.qt_misc_campaign_note_types(),
            type_buttons,
        )

        detail = QVBoxLayout()
        columns.addLayout(detail, 2)
        detail.setSpacing(7)
        self.heading = QLabel()
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        font = self.heading.font()
        font.setPointSizeF(font.pointSizeF() * 1.4)
        font.setBold(True)
        self.heading.setFont(font)
        detail.addWidget(self.heading)

        def section(title: str, hint: str) -> None:
            label = QLabel(title)
            font = label.font()
            font.setBold(True)
            label.setFont(font)
            detail.addWidget(label)
            description = QLabel(hint)
            description.setWordWrap(True)
            detail.addWidget(description)

        detail.addSpacing(6)
        section(
            self.trs.qt_misc_campaign_templates(),
            self.trs.qt_misc_campaign_template_help(),
        )
        self.templates = QListWidget()
        self.templates.setAccessibleName(self.trs.qt_misc_campaign_templates())
        detail.addWidget(self.templates, 1)
        detail.addSpacing(7)
        section(
            self.trs.qt_misc_campaign_fields(), self.trs.qt_misc_campaign_field_help()
        )
        controls = QHBoxLayout()
        detail.addLayout(controls)
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.trs.qt_misc_campaign_find())
        self.search.setClearButtonEnabled(True)
        controls.addWidget(self.search)
        self.include_fields = self._button(
            self.trs.qt_misc_campaign_include_all(),
            self.trs.qt_misc_campaign_fields(),
            controls,
        )
        self.exclude_fields = self._button(
            self.trs.qt_misc_campaign_exclude_all(),
            self.trs.qt_misc_campaign_fields(),
            controls,
        )
        self.fields = QListWidget()
        self.fields.setAccessibleName(self.trs.qt_misc_campaign_fields())
        detail.addWidget(self.fields, 3)
        self.count = QLabel()
        detail.addWidget(self.count)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)
        footer = QLabel(self.trs.qt_misc_campaign_saved())
        footer.setWordWrap(True)
        layout.addWidget(footer)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        save = buttons.addButton(
            self.trs.qt_misc_campaign_save(), QDialogButtonBox.ButtonRole.AcceptRole
        )
        save.setDefault(True)
        layout.addWidget(buttons)
        qconnect(buttons.accepted, self.accept)
        qconnect(buttons.rejected, self.reject)
        qconnect(self.types.currentRowChanged, self.show_type)
        qconnect(self.search.textChanged, self.filter_fields)
        qconnect(self.types.itemChanged, self.toggle_type)
        qconnect(self.include_types.clicked, lambda: self.set_all_types(True))
        qconnect(self.exclude_types.clicked, lambda: self.set_all_types(False))
        qconnect(self.include_fields.clicked, lambda: self.set_visible_fields(True))
        qconnect(self.exclude_fields.clicked, lambda: self.set_visible_fields(False))
        qconnect(
            self.fields.itemChanged, lambda _: self.update_choice("flds", self.fields)
        )
        qconnect(
            self.templates.itemChanged,
            lambda _: self.update_choice("tmpls", self.templates),
        )
        self.types.setCurrentRow(0)
        self.types.setFocus()

    @staticmethod
    def _state(checked: bool) -> Qt.CheckState:
        return Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked

    def _button(self, text: str, target: str, layout: QHBoxLayout) -> QPushButton:
        button = QPushButton(text)
        button.setAccessibleName(f"{text}: {target}")
        button.setAutoDefault(False)
        layout.addWidget(button)
        return button

    def show_type(self, row: int) -> None:
        if row < 0:
            return
        model = self.models[row]
        choice = self.choices[str(model["id"])]
        self.heading.setText(model["name"])
        self.search.clear()
        for group, widget in (("flds", self.fields), ("tmpls", self.templates)):
            widget.blockSignals(True)
            widget.clear()
            selected = choice[group]  # type: ignore[literal-required]
            for item in model[group]:
                key = _key(item)
                entry = QListWidgetItem(item["name"])
                entry.setData(Qt.ItemDataRole.UserRole, key)
                entry.setCheckState(self._state(key in selected))
                widget.addItem(entry)
            widget.blockSignals(False)
        # Excluded types keep their remembered choices for when they are
        # included again, but cannot be edited meanwhile.
        for control in (
            self.templates,
            self.search,
            self.include_fields,
            self.exclude_fields,
            self.fields,
            self.count,
        ):
            control.setEnabled(choice["include"])
        rows = min(3, self.templates.count())
        if rows:
            margins = self.templates.contentsMargins()
            self.templates.setFixedHeight(
                rows * self.templates.sizeHintForRow(0)
                + margins.top()
                + margins.bottom()
                + 4
            )
        self.update_count()

    def filter_fields(self, text: str) -> None:
        for index in range(self.fields.count()):
            item = self.fields.item(index)
            item.setHidden(text.casefold() not in item.text().casefold())

    def set_include(self, row: int, include: bool) -> None:
        model = self.models[row]
        choice = self.choices[str(model["id"])]
        # Including an excluded type with nothing selected would still share
        # nothing. Already-included types keep their selection.
        if (
            include
            and not choice["include"]
            and not choice["flds"]
            and not choice["tmpls"]
        ):
            choice["flds"] = _all_keys(model, "flds")
            choice["tmpls"] = _all_keys(model, "tmpls")
        choice["include"] = include

    def toggle_type(self, item: QListWidgetItem) -> None:
        row = self.types.row(item)
        self.set_include(row, item.checkState() == Qt.CheckState.Checked)
        if row == self.types.currentRow():
            self.show_type(row)

    def set_all_types(self, include: bool) -> None:
        self.types.blockSignals(True)
        for row in range(self.types.count()):
            self.set_include(row, include)
            self.types.item(row).setCheckState(self._state(include))
        self.types.blockSignals(False)
        self.show_type(self.types.currentRow())

    def set_visible_fields(self, checked: bool) -> None:
        self.fields.blockSignals(True)
        for index in range(self.fields.count()):
            item = self.fields.item(index)
            if not item.isHidden():
                item.setCheckState(self._state(checked))
        self.fields.blockSignals(False)
        self.update_choice("flds", self.fields)

    def update_choice(self, group: str, widget: QListWidget) -> None:
        mid = str(self.models[self.types.currentRow()]["id"])
        self.choices[mid][group] = [  # type: ignore[literal-required]
            item.data(Qt.ItemDataRole.UserRole)
            for index in range(widget.count())
            if (item := widget.item(index)).checkState() == Qt.CheckState.Checked
        ]
        self.update_count()

    def update_count(self) -> None:
        mid = str(self.models[self.types.currentRow()]["id"])
        self.count.setText(
            self.trs.qt_misc_campaign_selected(
                selected=len(self.choices[mid]["flds"]), total=self.fields.count()
            )
        )

    def accept(self) -> None:
        if self.mw.pm.profile is self.profile:
            self.profile[SHARING] = self.choices
            self.mw.pm.save()
        super().accept()
