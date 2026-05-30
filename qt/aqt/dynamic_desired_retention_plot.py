# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote

from aqt.qt import QDialog, Qt, QTimer, QVBoxLayout, QWidget, qconnect
from aqt.utils import disable_help_button
from aqt.webview import AnkiWebView


class DynamicDesiredRetentionPlotDialog(QDialog):
    silentlyClose = True

    def __init__(
        self,
        parent: QWidget,
        *,
        params: Sequence[float],
        calibration_weights: Sequence[float],
        calibration_avg_drs: Sequence[float],
        fsrs_equivalent_weights: Sequence[float] = (),
        fsrs_equivalent_drs: Sequence[float] = (),
        retention_min: float,
        retention_max: float,
        target_average_dr: float,
        save_target: Callable[[float], None],
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._save_target = save_target
        self.web: AnkiWebView | None = None
        self.setWindowTitle("Dynamic DR Plot")
        self.resize(760, 820)
        disable_help_button(self)

        self.web = AnkiWebView(self)
        self.web.set_bridge_command(self._on_bridge_command, self)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.web)
        self.setLayout(layout)

        payload: dict[str, Any] = {
            "params": list(params),
            "calibrationWeights": list(calibration_weights),
            "calibrationAvgDrs": list(calibration_avg_drs),
            "fsrsEquivalentWeights": list(fsrs_equivalent_weights),
            "fsrsEquivalentDrs": list(fsrs_equivalent_drs),
            "retentionMin": retention_min,
            "retentionMax": retention_max,
            "targetAverageDr": target_average_dr,
        }
        encoded_payload = quote(json.dumps(payload, separators=(",", ":")))
        self.web.load_sveltekit_page(
            f"dynamic-desired-retention-plot?payload={encoded_payload}"
        )

    def _on_bridge_command(self, command: str) -> None:
        if command == "dynamicDesiredRetentionPlotClose":
            QTimer.singleShot(0, self.reject)
            return

        if not command.startswith("save:"):
            return

        self._save_target(float(command.removeprefix("save:")))

    def reject(self) -> None:
        if self.web is not None:
            self.web.cleanup()
            self.web = None
        QDialog.reject(self)


def open_dynamic_desired_retention_plot(
    parent: QWidget,
    *,
    params: Sequence[float],
    calibration_weights: Sequence[float],
    calibration_avg_drs: Sequence[float],
    fsrs_equivalent_weights: Sequence[float] = (),
    fsrs_equivalent_drs: Sequence[float] = (),
    retention_min: float,
    retention_max: float,
    target_average_dr: float,
    save_target: Callable[[float], None],
) -> None:
    dialog = DynamicDesiredRetentionPlotDialog(
        parent,
        params=params,
        calibration_weights=calibration_weights,
        calibration_avg_drs=calibration_avg_drs,
        fsrs_equivalent_weights=fsrs_equivalent_weights,
        fsrs_equivalent_drs=fsrs_equivalent_drs,
        retention_min=retention_min,
        retention_max=retention_max,
        target_average_dr=target_average_dr,
        save_target=save_target,
    )
    setattr(parent, "_dynamic_desired_retention_plot_dialog", dialog)

    def clear_parent_reference() -> None:
        if getattr(parent, "_dynamic_desired_retention_plot_dialog", None) is dialog:
            setattr(parent, "_dynamic_desired_retention_plot_dialog", None)

    qconnect(dialog.finished, lambda _result: clear_parent_reference())
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
