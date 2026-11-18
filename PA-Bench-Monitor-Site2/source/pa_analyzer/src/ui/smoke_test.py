"""GUI smoke test — exercises the real Qt window end-to-end.

    python src/main.py --smoke-test   (or the .exe with --smoke-test)
Exit code 0 = all steps PASS. No dialogs are opened.
"""
from __future__ import annotations

import os
import tempfile
import traceback

import numpy as np
from matplotlib.backend_bases import KeyEvent, MouseEvent

from ui.main_window import MainWindow
from utils.paths import example_measurement_path

LAST_REPORT = ""

RAW_HEADER = ["SG Pout (dBm)", "SA Pin (dBm)", "DUT_Pin (dBm)", "DUT_Pout (dBm)", "IDC_Driver (mA)",
              "IDC_CA (mA)", "IDC_PK mA)", "Gain", "Pin(mW)", "Pout(mW)", "DC_Power(mW)",
              "DC_Power_Main(mW)", "PAE(%)", "DE(%)"]


def raw_tsv(pout, gain, pae) -> str:
    """Raw measurement table in the lab's column order, as Excel puts it on the clipboard."""
    lines = ["\t".join(RAW_HEADER)]
    for po, g, e in zip(pout, gain, pae):
        pin = po - g
        p_mw, pin_mw = 10 ** (po / 10), 10 ** (pin / 10)
        pdc = (p_mw - pin_mw) / max(e / 100, 0.01)
        lines.append("\t".join(f"{v:,.3f}" if abs(v) >= 1000 else f"{v:.3f}" for v in
                               [pin + 1.1, pin + 0.4, pin, po, 55.0, 300 + 2 * po, 20 + po, g, pin_mw, p_mw,
                                pdc, pdc * 0.6, e, e + 3]))
    return "\r\n".join(lines) + "\r\n"


def _fake():
    import PySide6
    return not hasattr(PySide6, "__version__")


def run_smoke_test(app, win: MainWindow) -> int:
    results = []
    c = win.c
    ax = lambda: win.renderer.ax  # noqa: E731
    win.resize(1400, 900)
    app.processEvents()

    def step(name, fn):
        try:
            fn()
            app.processEvents()
            results.append((name, True, ""))
        except Exception as exc:
            traceback.print_exc()
            results.append((name, False, repr(exc)))

    def ev(name, x, y, **kw):
        win.canvas.draw()
        px, py = ax().transData.transform((x, y))
        e = MouseEvent(name, win.canvas, px, py, **kw)
        win.canvas.callbacks.process(name, e)

    def only_current():
        """Test baseline: just the current dataset on the plot (as before 모두 해제 emptied the plot)."""
        win.set_all_checked(False)
        c.browsing = False
        c.checked = [c.sheet_name] if c.sheet_name else []
        win.refresh(rerender=True)

    def key(k):
        win.canvas.callbacks.process("key_press_event", KeyEvent("key_press_event", win.canvas, k))

    def item(name):
        return next(win.data_list.item(i) for i in range(win.data_list.count())
                    if win.data_list.item(i).data(256) == name)

    def lw_scale():
        from plotting.renderer import font_scale
        return max(font_scale(win.figure), 0.7)

    def curves():
        return [l.get_label() for l in ax().get_lines() if not l.get_label().startswith("_")]

    def Qt():
        from PySide6.QtCore import Qt as _Qt
        return _Qt

    # ------------------------------------------------------------------
    def open_example():
        win.open_file(example_measurement_path())
        assert c.measurement is not None and len(c.measurement.datasets) == 5
        kinds = [win.data_list.item(i).data(257) for i in range(win.data_list.count())]
        assert kinds.count("meas") == 6 and kinds.count("sim") == 5, kinds   # only the chosen set (Typical)

    def walk_datasets():
        for i in range(win.data_list.count()):
            win.data_list.setCurrentItem(win.data_list.item(i))
            app.processEvents()
        win.data_list.setCurrentItem(item("3.6GHz"))
        assert c.sheet_name == "3.6GHz" and c.checked == ["3.6GHz"], (c.sheet_name, c.checked)

    def conditions():
        items = dict(c.condition_panel_items())
        assert abs(c.current_freq_hz - 3.6e9) < 1, c.current_freq_hz
        assert list(items) == ["VG_DRV", "VG_CA", "VG_PK", "IQ_DRV", "IQ_CA"], items
        assert items["VG_CA"] == "-1.20 V" and items["VG_PK"] == "-1.35 V" and items["VG_DRV"] == "-1.10 V", items

    def overlay_single():
        assert "Measurement" in curves() and any(l.startswith("Simulation") for l in curves()), curves()

    def gain_graph():
        win.graph_buttons["gain_vs_pout"].setChecked(True)
        assert ax().get_ylim() == (20.0, 40.0) and ax().get_xlim() == (26.0, 52.0), (ax().get_xlim(), ax().get_ylim())
        win.graph_buttons["pae_vs_pout"].setChecked(True)
        assert ax().get_ylim() == (0.0, 70.0) and ax().get_xlim() == (26.0, 54.0)

    def axis_range():
        win.xmin_edit.setText("25"); win.xmax_edit.setText("45")
        win.ymin_edit.setText("10"); win.ymax_edit.setText("60")
        win.on_apply_range()
        assert ax().get_xlim() == (25.0, 45.0) and ax().get_ylim() == (10.0, 60.0)

    def left_drag_pan():
        before = ax().get_xlim()
        ev("button_press_event", 30, 20, button=1)
        ev("motion_notify_event", 33, 20)
        win.canvas.callbacks.process("button_release_event", MouseEvent("button_release_event", win.canvas, 0, 0, button=1))
        after = ax().get_xlim()
        assert after[0] < before[0] - 1, (before, after)
        assert abs(float(win.xmin_edit.text()) - after[0]) < 0.02

    def wheel_zoom():
        win.canvas.draw()
        b = ax().get_window_extent()
        before = ax().get_xlim()
        win.canvas.callbacks.process("scroll_event", MouseEvent("scroll_event", win.canvas, b.x0 + b.width / 2,
                                                                b.y0 + b.height / 2, step=1, button="up"))
        assert ax().get_xlim()[1] - ax().get_xlim()[0] < before[1] - before[0]

    def f_key_fit_all():
        key("f")
        x0, x1 = ax().get_xlim()
        assert x0 < 20.5 and x1 > 46.5, (x0, x1)            # every point of sim + meas visible
        win.on_reset()
        assert ax().get_xlim() == (26.0, 54.0)

    def hover():
        win.canvas.draw()
        line = [l for l in ax().get_lines() if l.get_label() == "Measurement"][0]
        px, py = ax().transData.transform((line.get_xdata()[10], line.get_ydata()[10]))
        win.canvas.callbacks.process("motion_notify_event", MouseEvent("motion_notify_event", win.canvas, px, py))
        assert win.hover.annot.get_visible() and "Pout =" in win.hover.annot.get_text()

    def markers_keys_snap():
        ev("motion_notify_event", 35.03, 30)
        key("v")
        ev("motion_notify_event", 40, 45.2)
        key("ㅗ")                                            # 'h' in Korean IME mode
        assert c.v_markers == [35.0] and c.cur_h == [45.0], (c.v_markers, c.cur_h)
        win.graph_buttons["gain_vs_pout"].setChecked(True)
        ev("motion_notify_event", 40, 30.47)
        key("h")
        assert c.cur_h == [30.5], c.cur_h                     # Gain: 0.1 dB
        win.graph_buttons["pae_vs_pout"].setChecked(True)
        assert c.cur_h == [45.0] and c.v_markers == [35.0]   # per-graph levels, shared Pout markers

    def drag_and_arrows():
        ev("button_press_event", 35.0, 30, button=1)
        assert c.selected == ("v", 0), c.selected
        ev("motion_notify_event", 37.26, 30)
        ev("button_release_event", 37.26, 30, button=1)
        assert c.v_markers == [37.3], c.v_markers             # 0.1 dBm grid
        for k in ("right", "right", "left", "right"):
            key(k)
        assert c.v_markers == [37.5], c.v_markers
        ev("button_press_event", 30, 45.0, button=1)
        ev("button_release_event", 30, 45.0, button=1)
        assert c.selected == ("h", 0)
        key("up"); key("up"); key("down")
        assert c.cur_h == [45.5], c.cur_h                     # PAE: 0.5 %
        assert any("Pout 37.50" in t.get_text() for t in [] ) or win.view.overlay.panel is not None

    def value_box_menu():
        win.canvas.draw()
        bb = win.view.overlay.panel.get_window_extent(win.canvas.get_renderer())
        assert win.view.overlay.panel_hit((bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2)
        menu, acts = win.build_panel_menu()
        acts["collapse"].trigger(); assert c.panel_collapsed
        acts["loc:lower right"].trigger(); assert c.panel_loc == "lower right"
        menu, acts = win.build_panel_menu()
        acts["expand"].trigger(); assert not c.panel_collapsed
        acts["hide"].trigger(); assert win.view.overlay.panel is None and not win.values_check.isChecked()
        menu, acts = win.build_marker_menu(("v", 0))
        acts["values"].trigger(); assert win.view.overlay.panel is not None
        key("delete")                                       # selected H marker removed
        assert c.cur_h == [], c.cur_h
        win.on_clear_markers()
        assert c.v_markers == []

    def multi_overlay():
        item("3.4GHz").setCheckState(Qt().Checked)
        item("FREQ_3P7G").setCheckState(Qt().Checked)
        names = curves()
        assert sum(1 for n in names if not n.startswith("Sim")) == 3, names
        assert sum(1 for n in names if n.startswith("Sim")) == 3, names
        assert any("FREQ_3P7G" in n for n in names)
        assert any("VG_CA = -1.20 V / -1.18 V" in t.get_text() for t in win.figure.texts), \
            [t.get_text() for t in win.figure.texts]
        win.data_list.setCurrentItem(item("3.4GHz"))          # selecting keeps the overlay
        assert len(c.checked) == 3

    def backoff():
        win.bo_edit.setText("6, 8")
        win.on_backoff()
        assert win.bo_table.rowCount() == 12, win.bo_table.rowCount()   # 3 meas + 3 sim, x2 BO
        r = c.backoff_rows()[0]
        assert abs(r["pout"] - (r["psat"] - 6)) < 1e-9 and 30 < r["pae"] < 60 and 25 < r["gain"] < 35, r
        assert any(l.get_label() == "_bo" for l in ax().get_lines())
        win.bo_edit.setText(""); win.on_backoff()
        only_current()
        assert len(c.visible_sheets()) == 1

    def raw_paste():
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain")
        _x, e = ds.xy("pout", "pae")
        n0 = len(c.measurement.sheets)
        block = ("Freq\tVDD main\tPulse Duty\tInput Loss\tOutput Loss\tVG_DRV\tVG_CA\tVG_PK\tIQ_DRV\tIQ_CA\r\n"
                 "3.6\t48\t10%\t0.35\t0.42\t-1.12\t-1.21\t-1.36\t54.5\t118.0\r\n\r\n")
        win.paste_text(block + raw_tsv(x, g, e), ask_freq=False, name="Board7 raw")
        r = c.sheet_result
        assert len(c.measurement.sheets) == n0 + 1 and r.label == "Board7 raw" and r.ok, r.messages
        assert r.raw.cell(r.header_row, r.mapping["pout"]) == "DUT_Pout (dBm)", r.mapping
        assert r.raw.cell(r.header_row, r.mapping["pae"]) == "PAE(%)"
        np.testing.assert_allclose(r.dataset.columns["pout"], x, atol=2e-3)
        np.testing.assert_allclose(r.dataset.columns["gain"], g, atol=2e-3)
        assert c.condition_panel_items() == [("VG_DRV", "-1.12 V"), ("VG_CA", "-1.21 V"), ("VG_PK", "-1.36 V"),
                                             ("IQ_DRV", "54.5 mA"), ("IQ_CA", "118.0 mA")], c.condition_panel_items()
        assert abs(c.current_freq_hz - 3.6e9) < 1
        assert r.sheet_name in c.checked and c.simulation_dataset() is not None
        # keep pasting: second board shows up next to the first
        win.paste_text(raw_tsv(x + 0.3, g - 0.4, e - 2), ask_freq=False, name="Board8 raw", freq_hz=3.6e9)
        assert {"Board7 raw", "Board8 raw"} <= {v.label for v in c.visible_sheets()}
        # the same name may be used again (e.g. re-measured board)
        win.paste_text(raw_tsv(x + 0.1, g - 0.1, e - 1), ask_freq=False, name="Board8 raw", freq_hz=3.6e9)
        labels = [v.label for v in c.visible_sheets()]
        assert labels.count("Board8 raw") == 2, labels
        assert len({v.sheet_name for v in c.visible_sheets()}) == len(labels)       # keys stay unique
        c.remove_sheet(c.sheet_name)
        win.refresh(rerender=True)
        assert sum(1 for n in curves() if "raw" in n) == 2, curves()

    def project_roundtrip():
        d = tempfile.mkdtemp()
        saved = c.save_project(os.path.join(d, "boards"))
        win.open_file(saved)
        labels = {v.label for v in c.visible_sheets()}
        assert c.measurement.path == saved and {"Board7 raw", "Board8 raw"} <= labels, labels

    def plot_panes():
        assert win.n_panes == 2 and [v.isVisible() for v in win.views[:2]] != [False, False]
        with c.using_pane(1):
            assert c.graph_key == "gain_vs_pout"
        g_ax = win.views[1].renderer.ax
        assert g_ax.get_ylabel() == "Gain (dB)", g_ax.get_ylabel()
        win.layout_combo.setCurrentIndex(win.layout_combo.findData(4))
        assert win.n_panes == 4
        for n in (1, 2, 3, 4):
            win.set_layout(n)
            if not _fake():
                app.processEvents()
                sizes = {(v.width(), v.height()) for v in win.visible_views}
                ws, hs = [w for w, _ in sizes], [h for _, h in sizes]
                assert max(ws) - min(ws) <= 2 and max(hs) - min(hs) <= 2, (n, sizes)
        # click pane 2 -> it becomes active; left-panel graph radio follows
        v2 = win.views[1]
        v2.canvas.draw()
        b = v2.renderer.ax.get_window_extent()
        for nm in ("button_press_event", "button_release_event"):
            v2.canvas.callbacks.process(nm, MouseEvent(nm, v2.canvas, b.x0 + 30, b.y0 + 30, button=1))
        assert c.active_pane == 1 and win.graph_buttons["gain_vs_pout"].isChecked()
        win.graph_buttons["pae_vs_pout"].setChecked(True)           # changes pane 2 only
        with c.using_pane(0):
            assert c.graph_key == "pae_vs_pout"
        with c.using_pane(1):
            assert c.graph_key == "pae_vs_pout"
        win.graph_buttons["gain_vs_pout"].setChecked(True)
        # markers live per pane
        for v in win.views:
            v.mouse = None
        win.target_view().add_v()
        with c.using_pane(1):
            assert len(c.v_markers) == 1
        with c.using_pane(0):
            assert c.v_markers == []
        win.on_clear_markers()
        win.activate_view(win.views[0])
        assert c.active_pane == 0 and win.graph_buttons["pae_vs_pout"].isChecked()
        from PySide6.QtGui import QGuiApplication
        win.on_copy_all()
        img = QGuiApplication.clipboard().image()
        assert not img.isNull() and img.width() > 2 * 200

    def dataset_style_menu():
        win.data_list.setCurrentItem(item("3.6GHz"))
        only_current()
        menu, acts = win.build_data_menu()
        acts["color:#2ca02c"].trigger()
        acts["style:dashed"].trigger()
        acts["width:thick"].trigger()
        acts["marker:s"].trigger()
        line = [l for l in ax().get_lines() if l.get_label() == "Measurement"][0]
        k = lw_scale()
        assert (line.get_color(), line.get_linestyle(), line.get_marker()) == ("#2ca02c", "--", "s") and \
            abs(line.get_linewidth() - 2.4 * k) < 1e-6, (line.get_color(), line.get_linestyle(), line.get_linewidth(), k)
        g_line = [l for l in win.views[1].renderer.ax.get_lines() if l.get_label() == "Measurement"][0]
        assert g_line.get_color() == "#2ca02c"                      # every pane follows
        menu, acts = win.build_data_menu()
        acts["marker:none"].trigger()
        assert [l for l in ax().get_lines() if l.get_label() == "Measurement"][0].get_marker() in ("None", "", None)
        acts["reset"].trigger()
        line = [l for l in ax().get_lines() if l.get_label() == "Measurement"][0]
        assert line.get_linestyle() == "-" and line.get_marker() == "o"

    def multi_select_style():
        win.set_layout(2)
        win.set_all_checked(True)
        names = ["3.4GHz", "3.5GHz"]
        menu, acts = win.build_data_menu(names)
        acts["color:#000000"].trigger()
        acts["width:thick"].trigger()
        acts["style:dotted"].trigger()
        lines = {l.get_label().split(" ·")[0]: l for l in ax().get_lines() if not l.get_label().startswith(("_", "Sim"))}
        for n in names:
            assert lines[n].get_color() == "#000000" and abs(lines[n].get_linewidth() - 2.4 * lw_scale()) < 1e-6 and lines[n].get_linestyle() == ":"
        assert lines["3.6GHz"].get_color() != "#000000"
        acts["reset"].trigger()

    def copy_all_and_conditions():
        from PySide6.QtGui import QGuiApplication
        win.set_layout(2)
        win.on_copy_one()
        one = QGuiApplication.clipboard().image()
        win.on_copy_plot()                                    # default = whole screen
        allimg = QGuiApplication.clipboard().image()
        assert allimg.width() >= 2 * one.width() - 2, (allimg.width(), one.width())
        p = win.save_all_png(os.path.join(tempfile.mkdtemp(), "all"))
        assert p.endswith(".png") and os.path.getsize(p) > 20000
        win.set_condition_keys(["freq", "vgs_ca", "iq_ca"])
        assert [k for k, _ in c.condition_panel_items()] == ["FREQ", "VG_CA", "IQ_CA"]
        from core.quantities import DEFAULT_DISPLAY_CONDITIONS
        win.set_condition_keys(list(DEFAULT_DISPLAY_CONDITIONS))

    def sim_checklist():
        only_current()
        win.data_list.setCurrentItem(item("3.6GHz"))
        sims = [n for n in curves() if n.startswith("Sim")]
        assert sims == ["Simulation (Typical)"], curves()
        item("sim:Typical @ 3.60 GHz").setCheckState(Qt().Unchecked)      # user hides the matched sim
        assert not any(n.startswith("Sim") for n in curves()), curves()
        win.sim_combo.setCurrentIndex(win.sim_combo.findData("Corner SS"))  # choose another set in the box
        listed = [win.data_list.item(i).data(256) for i in range(win.data_list.count())
                  if win.data_list.item(i).data(257) == "sim"]
        assert all(n.startswith("sim:Corner SS") for n in listed) and len(listed) == 4, listed
        assert curves()[0] == "Simulation (Corner SS)", curves()
        win.sim_combo.setCurrentIndex(win.sim_combo.findData("*"))          # all sets -> grouped list
        item("sim:Typical @ 3.60 GHz").setCheckState(Qt().Checked)
        assert sum(1 for n in curves() if n.startswith("Sim")) == 2, curves()
        win.sim_combo.setCurrentIndex(win.sim_combo.findData("Typical"))
        c.sim_on, c.sim_off = [], []
        win.refresh(rerender=True)
        win.sim_enable_check.setChecked(False)
        assert not any(n.startswith("Sim") for n in curves())
        win.sim_enable_check.setChecked(True)
        win.sim_auto_check.setChecked(False)
        c.sim_on, c.sim_off = [], []
        win.refresh(rerender=True)
        assert not any(n.startswith("Sim") for n in curves())
        win.sim_auto_check.setChecked(True)
        assert any(n.startswith("Sim") for n in curves())

    def colors_by_frequency():
        only_current()
        c.checked = ["3.4GHz", "3.6GHz", "FREQ_3P7G"]
        win.refresh(rerender=True)
        lines = {l.get_label(): l.get_color() for l in ax().get_lines() if not l.get_label().startswith("_")}
        for f in ("3.40", "3.60", "3.70"):
            meas = [col for lab, col in lines.items() if not lab.startswith("Sim") and f"{f} GHz" in lab]
            sim = [col for lab, col in lines.items() if lab.startswith("Sim") and f"{f} GHz" in lab]
            assert meas and sim and meas[0] == sim[0], (f, lines)                 # meas = sim colour
        assert len(set(lines.values())) == 3, lines                               # 3 FREQs, 3 colours
        # a second measurement at 3.6 GHz: same colour, different marker
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain"); _x, e = ds.xy("pout", "pae")
        win.paste_text(raw_tsv(x + 0.2, g - 0.3, e - 1), ask_freq=False, name="Board2", freq_hz=3.6e9)
        b2 = c.sheet_name
        l36 = [l for l in ax().get_lines() if "3.60 GHz" in l.get_label() and not l.get_label().startswith("Sim")]
        assert len(l36) == 2 and l36[0].get_color() == l36[1].get_color() and l36[0].get_marker() != l36[1].get_marker()
        # user colour on a measurement -> the simulation at that FREQ follows
        c.set_styles(["3.6GHz"], color="#000000")
        win.refresh(rerender=True)
        sim36 = [l for l in ax().get_lines() if l.get_label().startswith("Sim") and "3.60 GHz" in l.get_label()][0]
        assert sim36.get_color() == "#000000"
        c.reset_dataset_style("3.6GHz")
        c.remove_sheet(b2)
        only_current()

    def highlight_selected():
        only_current()
        c.checked = ["3.4GHz", "3.6GHz", "FREQ_3P7G"]
        win.refresh(rerender=True)
        win.data_list.setCurrentItem(item("3.6GHz"))       # select a measurement in the list
        win.refresh(rerender=True)
        assert c.checked == ["3.4GHz", "3.6GHz", "FREQ_3P7G"], c.checked   # overlay kept
        ln = {l.get_label(): l for l in ax().get_lines() if not l.get_label().startswith("_")}
        sel = [l for lab, l in ln.items() if "3.60 GHz" in lab and not lab.startswith("Sim")][0]
        others = [l for lab, l in ln.items() if "3.60 GHz" not in lab]
        pair = [l for lab, l in ln.items() if "3.60 GHz" in lab and lab.startswith("Sim")][0]
        assert (sel.get_alpha() or 1) == 1 and sel.get_zorder() == 10, sel.get_zorder()
        assert others and all((l.get_alpha() or 1) < 0.3 for l in others), [l.get_alpha() for l in others]
        assert (pair.get_alpha() or 1) > 0.5                               # its own sim stays readable
        sim34 = [d for d in c.visible_sims() if abs(d.freq_hz - 3.4e9) < 1e6][0]
        win.data_list.setCurrentItem(item("sim:" + sim34.name))            # select a simulation row
        win.refresh(rerender=True)
        ln = {l.get_label(): l for l in ax().get_lines() if not l.get_label().startswith("_")}
        s34 = [l for lab, l in ln.items() if lab.startswith("Sim") and "3.40 GHz" in lab][0]
        m36 = [l for lab, l in ln.items() if "3.60 GHz" in lab and not lab.startswith("Sim")][0]
        assert (s34.get_alpha() or 1) == 1 and s34.get_zorder() == 9 and (m36.get_alpha() or 1) < 0.3
        assert win.data_list.currentItem().data(win.KIND_ROLE) == "sim"   # list keeps the sim row selected
        win.data_list.clear_choice()                                       # empty space clicked -> all equal
        win.refresh(rerender=True)
        alphas = [l.get_alpha() or 1 for l in ax().get_lines() if not l.get_label().startswith("_")]
        assert all(a > 0.5 for a in alphas) and max(l.get_zorder() for l in ax().get_lines()
                                                    if not l.get_label().startswith("_")) < 9, alphas
        assert win.data_list.currentItem() is None and not win.data_list.selectedItems()
        win.data_list.setCurrentItem(item("3.6GHz"))                       # choosing again -> emphasis back
        win.refresh(rerender=True)
        m36 = [l for l in ax().get_lines() if "3.60 GHz" in l.get_label() and not l.get_label().startswith("Sim")][0]
        assert m36.get_zorder() == 10
        win.hl_check.setChecked(False)                                     # option off -> all normal
        if _fake():
            win.on_highlight_toggle(False)
        assert all((l.get_alpha() or 1) > 0.5 for l in ax().get_lines()
                   if not l.get_label().startswith("_") and not l.get_label().startswith("Sim"))
        win.hl_check.setChecked(True)
        if _fake():
            win.on_highlight_toggle(True)
        c.highlight_sim = None
        only_current()

    def list_keeps_scroll():
        bar = win.data_list.verticalScrollBar()
        if not _fake():
            win.resize(win.width(), 500)                  # short list window -> scroll bar needed
            app.processEvents()
        win.on_filter_changed("")
        win.refresh(rerender=True)
        bar.setValue(max(1, bar.maximum() // 2) if not _fake() else 7)
        pos = bar.value()
        it = next(win.data_list.item(i) for i in range(win.data_list.count() - 1, -1, -1)
                  if win.data_list.item(i).data(win.KIND_ROLE) == "meas")
        win.data_list.setCurrentItem(it)                  # choosing a dataset rebuilds the list
        win.refresh(rerender=True)
        app.processEvents()
        assert bar.value() == pos, (bar.value(), pos)     # ... but the view stays where it was
        only_current()

    def plot_click_pick_and_clear():
        only_current()
        c.checked = ["3.4GHz", "3.6GHz", "FREQ_3P7G"]
        win.data_list.setCurrentItem(item("3.4GHz"))
        win.refresh(rerender=True)
        line = [l for l in ax().get_lines() if l.get_gid() == "3.6GHz"][0]
        xd, yd = line.get_data()
        import numpy as _np
        others = [l for l in ax().get_lines() if l.get_gid() and l is not line]
        def gap(i):        # pixel distance from point i of the 3.6 GHz curve to every other curve
            p = ax().transData.transform((xd[i], yd[i]))
            return min(_np.min(_np.hypot(*(ax().transData.transform(_np.column_stack(o.get_data())) - p).T))
                       for o in others)
        x0, x1 = ax().get_xlim(); y0, y1 = ax().get_ylim()
        inside = [i for i in range(len(xd)) if x0 < xd[i] < x1 and y0 < yd[i] < y1]
        k = max(inside, key=gap)
        for nm in ("button_press_event", "button_release_event"):     # click ON the 3.6 GHz curve
            ev(nm, float(xd[k]), float(yd[k]), button=1)
        win.refresh(rerender=True)
        assert c.sheet_name == "3.6GHz" and c.checked == ["3.4GHz", "3.6GHz", "FREQ_3P7G"], (c.sheet_name, c.checked)
        assert win.data_list.currentItem().data(256) == "3.6GHz"           # list follows the plot
        assert [l for l in ax().get_lines() if l.get_gid() == "3.6GHz"][0].get_zorder() == 10
        sim = [l for l in ax().get_lines() if str(l.get_gid()).startswith("sim:") and "3.40" in l.get_label()][0]
        sx, sy = sim.get_data()
        for nm in ("button_press_event", "button_release_event"):     # click a simulation curve
            ev(nm, float(sx[len(sx) // 3]), float(sy[len(sy) // 3]), button=1)
        win.refresh(rerender=True)
        assert c.highlight_sim == sim.get_gid() and win.data_list.currentItem().data(256) == sim.get_gid()
        xl, yl = ax().get_xlim(), ax().get_ylim()
        for nm in ("button_press_event", "button_release_event"):     # click empty space -> all equal
            ev(nm, xl[0] + 0.02 * (xl[1] - xl[0]), yl[1] - 0.03 * (yl[1] - yl[0]), button=1)
        win.refresh(rerender=True)
        assert c.highlight_cleared and all((l.get_alpha() or 1) > 0.5 for l in ax().get_lines()
                                           if l.get_gid() and not str(l.get_gid()).startswith("sim:"))
        assert not win.data_list.selectedItems()
        assert ax().get_xlim() == xl                                       # a click is not a pan
        only_current()

    def search_filter():
        win.on_filter_changed("3.6")
        shown = [win.data_list.item(i) for i in range(win.data_list.count())
                 if win.data_list.item(i).data(win.KIND_ROLE) in ("meas", "sim") and not win.data_list.item(i).isHidden()]
        meas_shown = [i for i in shown if i.data(win.KIND_ROLE) == "meas"]
        assert meas_shown and all("3.6" in (i.text() + i.data(256)) for i in meas_shown), [i.text() for i in meas_shown]
        all_sims = [win.data_list.item(i) for i in range(win.data_list.count())
                    if win.data_list.item(i).data(win.KIND_ROLE) == "sim"]
        assert all_sims and all(not i.isHidden() for i in all_sims)    # simulations always listed
        hidden_meas = [win.data_list.item(i).data(256) for i in range(win.data_list.count())
                       if win.data_list.item(i).data(win.KIND_ROLE) == "meas" and win.data_list.item(i).isHidden()]
        assert "3.4GHz" in hidden_meas
        win.on_filter_show_only()                                          # Enter / [결과만 Plot]
        assert c.checked and all("3.6" in n or "3.6" in c.measurement.sheet_label(n) for n in c.checked), c.checked
        assert all(abs(d.freq_hz - 3.6e9) < 1e6 for d in c.visible_sims()), [d.name for d in c.visible_sims()]
        win.on_filter_changed("3.6 nothing-like-this")                     # several words = all must match
        assert not any(not win.data_list.item(i).isHidden() for i in range(win.data_list.count())
                       if win.data_list.item(i).data(win.KIND_ROLE) == "meas")
        # names given when pasting (and renamed datasets) are searchable too
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain"); _x, e = ds.xy("pout", "pae")
        keys = []
        for nm in ("보드A_VG-1.2", "보드B_VG-1.4"):
            win.paste_text(raw_tsv(x, g, e), ask_freq=False, name=nm, freq_hz=3.5e9)
            keys.append(c.sheet_name)
        win.on_filter_changed("보드a")
        vis = [win.data_list.item(i).data(256) for i in range(win.data_list.count())
               if win.data_list.item(i).data(win.KIND_ROLE) == "meas" and not win.data_list.item(i).isHidden()]
        assert vis == [keys[0]], vis
        win.on_filter_changed("보드")
        win.on_filter_show_only()
        assert sorted(c.checked) == sorted(keys), c.checked
        win.paste_text(raw_tsv(x, g, e), ask_freq=False, name="다른이름", freq_hz=3.5e9)   # pasted while searching
        assert win._filter == "보드" and item(c.sheet_name).isHidden()                    # -> the search stays
        assert c.sheet_name in {l.get_gid() for l in ax().get_lines()} or c.sheet_name in c.checked
        keys.append(c.sheet_name)
        for k in keys:
            c.remove_sheet(k)
        win.on_filter_changed("")
        assert not item("3.4GHz").isHidden()
        only_current()
        c.sim_on, c.sim_off = [], []

    def filter_select_color():
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain"); _x, e = ds.xy("pout", "pae")
        keys = []
        for i in range(16):                                    # many datasets -> compact legend
            win.paste_text(raw_tsv(x + 0.05 * i, g - 0.05 * i, e - i * 0.2), ask_freq=False,
                           name=f"LotA_{i}" if i < 4 else f"LotB_{i}", freq_hz=(3.3 + (i % 4) * 0.1) * 1e9)
            keys.append(c.sheet_name)
        win.refresh(rerender=True)
        n_curves = len([l for l in ax().get_lines() if l.get_gid()])
        leg = [t.get_text() for t in ax().get_legend().get_texts()]
        assert n_curves > 14 and len(leg) < n_curves and any("GHz" in t for t in leg), (n_curves, leg)
        win.on_filter_changed("lota")                          # search, then Ctrl+A = only the results
        win.data_list.selectAll()
        sel = [i.data(256) for i in win.data_list.selectedItems() if not i.isHidden()]
        assert sorted(sel) == sorted(keys[:4]), sel
        assert all(not i.isHidden() for i in win.data_list.selectedItems())
        menu, acts = win.build_data_menu(sel)
        acts["color:distinct"].trigger()                       # each a different colour
        cols = [c.style_of_dataset(k).get("color") for k in keys[:4]]
        assert len(set(cols)) == 4 and all(cols), cols
        drawn = {l.get_gid(): l.get_color() for l in ax().get_lines() if l.get_gid() in keys[:4]}
        assert len(set(drawn.values())) == 4, drawn
        menu, acts = win.build_data_menu(sel)
        acts["color:#000000"].trigger()                        # all the same colour
        assert all(c.style_of_dataset(k).get("color") == "#000000" for k in keys[:4])
        assert c.style_of_dataset(keys[5]).get("color") is None  # not selected -> unchanged
        acts["width:thick"].trigger()
        menu, acts = win.build_data_menu(sel)
        acts["color:auto"].trigger()                           # back to FREQ colours, width kept
        assert all(not c.style_of_dataset(k).get("color") and c.style_of_dataset(k).get("linewidth") == 2.4
                   for k in keys[:4]), [c.style_of_dataset(k) for k in keys[:4]]
        for i in range(win.data_list.count()):                 # a hidden row selected must never be deleted
            win.data_list.item(i).setSelected(win.data_list.item(i).data(256) in (keys[0], keys[5]))
        if _fake():
            win.on_delete_datasets(confirm=False)
            assert c.measurement.sheet(keys[5]) is not None and c.measurement.sheet(keys[0]) is None
            keys.remove(keys[0])
        win.on_filter_changed("")
        for k in keys:
            c.remove_sheet(k)
        only_current()

    def arrow_keys_held():
        from PySide6 import QtCore
        queued, orig = [], QtCore.QTimer.singleShot
        renders = []
        orig_refresh = win.refresh
        try:
            QtCore.QTimer.singleShot = staticmethod(lambda ms, fn: queued.append(fn))   # hold the timers
            win.refresh = lambda *a, **k: (renders.append(1), orig_refresh(*a, **k))
            meas = [win.data_list.item(i) for i in range(win.data_list.count())
                    if win.data_list.item(i).data(win.KIND_ROLE) == "meas"]
            for it in meas + meas[::-1]:                  # ↓↓↓…↑↑↑ held down: many changes quickly
                win.data_list.setCurrentItem(it)
            last = meas[0].data(256)
            assert c.sheet_name == last                    # the selection itself follows at once
            for fn in queued:                              # the timers fire after the keys stop
                fn()
            assert len(renders) == 1, len(renders)         # ... and the plot is drawn ONCE, not 2N times
        finally:
            QtCore.QTimer.singleShot = orig
            win.refresh = orig_refresh
        win.refresh(rerender=True)

    def show_also():
        only_current()
        c.checked = ["3.4GHz"]; c.sheet_name = "3.4GHz"
        win.refresh(rerender=True)
        menu, acts = win.build_data_menu(["3.6GHz", "FREQ_3P7G"])
        acts["add"].trigger()                            # added on top of what is shown
        assert c.checked == ["3.4GHz", "3.6GHz", "FREQ_3P7G"], c.checked
        gids = {l.get_gid() for l in ax().get_lines() if l.get_gid()}
        assert {"3.4GHz", "3.6GHz", "FREQ_3P7G"} <= gids, gids
        c.checked = []; c.sheet_name = "3.4GHz"          # nothing checked (browse one) -> current one stays
        win.build_data_menu(["3.5GHz"])[1]["add"].trigger()
        assert c.checked == ["3.4GHz", "3.5GHz"], c.checked
        only_current()

    def hide_selected():
        c.checked = ["3.4GHz", "3.5GHz", "3.6GHz", "FREQ_3P7G"]
        win.refresh(rerender=True)
        sim36 = "sim:" + [d for d in c.visible_sims() if abs(d.freq_hz - 3.6e9) < 1e6][0].name
        menu, acts = win.build_data_menu(["3.4GHz", "3.6GHz", sim36])
        assert "hide" in acts and "only" in acts
        acts["hide"].trigger()
        assert c.checked == ["3.5GHz", "FREQ_3P7G"], c.checked
        gids = {l.get_gid() for l in ax().get_lines() if l.get_gid()}
        assert "3.4GHz" not in gids and "3.6GHz" not in gids and sim36 not in gids and "3.5GHz" in gids, gids
        c.checked = ["3.4GHz"]; c.sheet_name = "3.4GHz"
        menu, acts = win.build_data_menu(["3.4GHz"])
        acts["hide"].trigger()                          # hiding the last one: it must not stay on the plot
        assert "3.4GHz" not in {l.get_gid() for l in ax().get_lines()}
        c.sim_on, c.sim_off = [], []
        only_current()

    def batch_rename():
        import ui.main_window as mw
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain"); _x, e = ds.xy("pout", "pae")
        keys = []
        for i, hz in enumerate((3.5e9, 3.6e9, 3.7e9)):
            win.paste_text(raw_tsv(x, g - i, e), ask_freq=False, name=f"tmp{i}", freq_hz=hz)
            keys.append(c.sheet_name)
        orig = mw.QInputDialog.getText
        try:
            mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("VG -1.4 튜닝후", True))
            menu, acts = win.build_data_menu(keys)
            acts["rename_many"].trigger()
            win.refresh(rerender=True)
            labels = [c.measurement.sheet(k).label for k in keys]
            assert labels == ["VG -1.4 튜닝후"] * 3, labels           # exactly the typed name, nothing added
            mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("x", False))   # Cancel -> unchanged
            win.build_data_menu(keys)[1]["rename_many"].trigger()
            assert [c.measurement.sheet(k).label for k in keys] == ["VG -1.4 튜닝후"] * 3
            win.on_filter_changed("튜닝후")                           # the new name is searchable
            vis = [win.data_list.item(i).data(256) for i in range(win.data_list.count())
                   if win.data_list.item(i).data(win.KIND_ROLE) == "meas" and not win.data_list.item(i).isHidden()]
            assert sorted(vis) == sorted(keys), vis
            win.on_filter_changed("")
            assert "rename_many" not in win.build_data_menu([keys[0]])[1]   # single item: normal rename
        finally:
            mw.QInputDialog.getText = orig
            for k in keys:
                c.remove_sheet(k)
            only_current()

    def drag_value_box():
        only_current()
        c.v_markers = [38.0]
        c.show_marker_values = True
        win.refresh(rerender=True)
        v = win.views[0]
        ov = v.overlay
        assert ov.panel is not None
        win.canvas.draw()
        bb = ov.panel.get_window_extent(win.canvas.get_renderer())
        x0, y0 = (bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2
        win._set_panel(loc="lower right")
        win.canvas.draw()
        bb = ov.panel.get_window_extent(win.canvas.get_renderer())
        x0, y0 = (bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2
        old_corner = ov.panel_corner()
        win.canvas.callbacks.process("button_press_event", MouseEvent("button_press_event", win.canvas, x0, y0, button=1))
        win.canvas.callbacks.process("motion_notify_event", MouseEvent("motion_notify_event", win.canvas, x0 - 150, y0 + 90))
        win.canvas.callbacks.process("button_release_event", MouseEvent("button_release_event", win.canvas, x0 - 150, y0 + 90, button=1))
        assert c.panel_xy is not None
        win.canvas.draw()
        new_corner = ov.panel_corner()
        ax_bb = ax().bbox
        assert abs((old_corner[0] - new_corner[0]) * ax_bb.width - 150) < 3, (old_corner, new_corner)   # moved left
        assert abs((new_corner[1] - old_corner[1]) * ax_bb.height - 90) < 3, (old_corner, new_corner)   # and up
        for nm, dx in (("button_press_event", 0), ("motion_notify_event", 5000), ("button_release_event", 5000)):
            bb = ov.panel.get_window_extent(win.canvas.get_renderer())
            cx, cy = (bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2
            kw = {"button": 1} if nm != "motion_notify_event" else {}
            win.canvas.callbacks.process(nm, MouseEvent(nm, win.canvas, cx + dx, cy, **kw))
        win.canvas.draw()
        bb = ov.panel.get_window_extent(win.canvas.get_renderer())
        assert bb.x1 <= ax().bbox.x1 + 2, (bb.x1, ax().bbox.x1)          # pulled far right: stays inside
        new_corner = ov.panel_corner()
        assert c.v_markers == [38.0]                       # dragging the box does not move the marker
        win.refresh(rerender=True)                         # position survives a redraw
        assert ov.panel_corner() == new_corner or abs(ov.panel_corner()[0] - new_corner[0]) < 1e-3
        win._set_panel(loc="upper right")                  # choosing a corner again resets the drag
        assert c.panel_xy is None
        c.v_markers = []
        win.refresh(rerender=True)

    def multi_highlight():
        only_current()
        c.checked = ["3.4GHz", "3.5GHz", "3.6GHz", "FREQ_3P7G"]
        win.refresh(rerender=True)
        for i in range(win.data_list.count()):
            it = win.data_list.item(i)
            it.setSelected(it.data(256) in ("3.4GHz", "3.6GHz"))
        win.on_data_selection()                          # (real Qt: emitted by the list)
        win.refresh(rerender=True)
        ln = {l.get_gid(): l for l in ax().get_lines() if l.get_gid()}
        assert ln["3.4GHz"].get_zorder() == 10 and ln["3.6GHz"].get_zorder() == 10, \
            (ln["3.4GHz"].get_zorder(), ln["3.6GHz"].get_zorder())
        assert (ln["3.5GHz"].get_alpha() or 1) < 0.3 and (ln["FREQ_3P7G"].get_alpha() or 1) < 0.3
        sims = {k: l for k, l in ln.items() if k.startswith("sim:")}
        s34 = [l for l in sims.values() if "3.40" in l.get_label()][0]
        s35 = [l for l in sims.values() if "3.50" in l.get_label()][0]
        assert (s34.get_alpha() or 1) > 0.5 and (s35.get_alpha() or 1) < 0.3    # partners stay, others fade
        win.data_list.clear_choice()                     # empty space -> all equal again
        win.refresh(rerender=True)
        assert all((l.get_alpha() or 1) > 0.5 for k, l in
                   {l.get_gid(): l for l in ax().get_lines() if l.get_gid()}.items() if not k.startswith("sim:"))
        assert "QListWidget::item:selected:!active" in win.data_list.styleSheet() if not _fake() else True
        only_current()

    def pick_scrolls_list():
        """Clicking a curve whose row is scrolled out of view brings that row into view
        (timers held back like real Qt: the old 'restore scroll' must not win afterwards)."""
        from PySide6 import QtCore
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain"); _x, e = ds.xy("pout", "pae")
        keys = []
        for i in range(25):
            win.paste_text(raw_tsv(x, g - 0.2 * i, e), ask_freq=False, name=f"far{i}", freq_hz=3.6e9)
            keys.append(c.sheet_name)
        win.refresh(rerender=True)
        bar = win.data_list.verticalScrollBar()
        bar.setValue(0)                                           # list at the top, target far below
        target = keys[-1]
        queued, orig = [], QtCore.QTimer.singleShot
        try:
            QtCore.QTimer.singleShot = staticmethod(lambda ms, fn: queued.append(fn))
            win.on_plot_pick(target)
            while queued:                                         # run every pending timer in order
                queued.pop(0)()
        finally:
            QtCore.QTimer.singleShot = orig
        it = item(target)
        if _fake():
            idx = [win.data_list.item(i) for i in range(win.data_list.count())].index(it)
            assert bar.value() == idx, (bar.value(), idx)
        else:
            assert win.data_list.visualItemRect(it).intersects(win.data_list.viewport().rect())
        assert win.data_list.currentItem() is it
        for k in keys:
            c.remove_sheet(k)
        only_current()

    def color_buttons():
        only_current()
        c.checked = ["3.4GHz", "3.5GHz", "3.6GHz"]
        win.refresh(rerender=True)
        win.on_colors_distinct()
        cols = [c.style_of_dataset(n).get("color") for n in c.checked]
        assert len(set(cols)) == 3 and all(cols), cols
        drawn = {l.get_gid(): l.get_color() for l in ax().get_lines() if l.get_gid() in c.checked}
        assert len(set(drawn.values())) == 3
        win.on_colors_by_freq()
        assert not any(c.style_of_dataset(n).get("color") for n in c.checked)
        assert win.axis_reset_btn.width() == win.colors_distinct_btn.width() == win.colors_freq_btn.width() \
            if not _fake() else True
        only_current()

    def data_editor():
        import ui.data_editor as de
        # the real dialog: paste a block, add / delete rows, read back
        dlg = de.DataEditorDialog("t", [["Pout", "Gain", "PAE"], [30.0, 14.0, 20.0]], name="x")
        dlg.table.setCurrentCell(1, 0)
        dlg.paste("31\t13.9\t22\n32\t13.5\t25")
        assert dlg.values() == ("x", [["Pout", "Gain", "PAE"], [31.0, 13.9, 22.0], [32.0, 13.5, 25.0]]), dlg.values()
        dlg.table.setCurrentCell(2, 0)
        dlg.add_row()
        assert dlg.table.rowCount() == 4
        only_current()
        c.checked = ["3.4GHz", "3.6GHz"]
        win.refresh(rerender=True)
        win._history_flush()
        r = c.measurement.sheet("3.6GHz")
        pae_col, pout_col = r.mapping["pae"], r.mapping["pout"]
        seen = {}

        class Fake:
            accept = True

            def __init__(self, title, rows, **kw):
                seen.update(kw, rows=[list(x) for x in rows], title=title)

            def exec(self):
                return Fake.accept

            def values(self):
                rows = [list(x) for x in seen["rows"]]
                i = (seen.get("header_row") or 0) + 3
                rows[i][pae_col] = 99.0                        # change one PAE value
                return "보정후", [[de.to_cell(de.fmt_cell(v)) for v in row] for row in rows]
        orig = de.DataEditorDialog
        try:
            de.DataEditorDialog = Fake
            win.data_list.on_open("3.6GHz")                    # = double-click in the list
            assert c.measurement.sheet("3.6GHz").label == "보정후"
            ln = [l for l in ax().get_lines() if l.get_gid() == "3.6GHz"][0]
            assert 99.0 in list(ln.get_ydata()), ln.get_ydata()
            assert seen.get("mark_cols") and pae_col in seen["mark_cols"]  # plotted columns coloured
            win._history_flush()
            win.on_undo()                                      # Ctrl+Z: old numbers + name back
            ln = [l for l in ax().get_lines() if l.get_gid() == "3.6GHz"][0]
            assert 99.0 not in list(ln.get_ydata()) and c.measurement.sheet("3.6GHz").label != "보정후"
            # double-click ON the curve in the plot -> editor at the clicked Pout
            xs, ys = ln.get_xdata(), ln.get_ydata()
            k = len(xs) // 2
            seen.clear()
            Fake.accept = False                                # look only (Cancel): nothing changes
            ev("button_press_event", float(xs[k]), float(ys[k]), button=1, dblclick=True)
            assert seen.get("focus_row") is not None, seen.keys()
            assert abs(seen["rows"][seen["focus_row"]][pout_col] - xs[k]) < 0.6
            assert c.measurement.sheet("3.6GHz").label != "보정후"           # Cancel = unchanged
            seen.clear()                                       # simulation: read-only view
            sim = [d for d in c.visible_sims()][0]
            win.on_open_dataset("sim:" + sim.name)
            assert seen.get("readonly") is True and seen["rows"]
        finally:
            de.DataEditorDialog = orig
        only_current()

    def editor_formulas():
        import ui.data_editor as de
        rows = [["Pout", "Gain", "PAE"], [30.0, 14.0, 20.0], [31.0, 13.9, 22.0], [32.0, 13.5, 25.0], [33.0, 13.0, 28.0]]
        dlg = de.DataEditorDialog("t", rows, name="x")
        t = dlg.table
        t.editor = None
        assert t.picking() is False                    # clicking a cell with no editor open: no error

        def type_into(r, c, text):                     # like typing into a cell and pressing Enter
            it = t.item(r, c)
            it.setText(text)
            dlg._on_item_changed(it)
        dlg.add_column()                                # [열 추가] -> column D
        assert t.columnCount() == 4
        type_into(1, 3, "=A2+B2")                       # D2 = Pout + Gain
        assert t.item(1, 3).text() == "44", t.item(1, 3).text()
        type_into(1, 2, "=(A2-10)*2/4")                 # overwrite PAE with a calculation
        assert t.item(1, 2).text() == "10", t.item(1, 2).text()
        assert t.item(1, 3).text() == "44"              # D2 does not use C2
        dlg.fill((1, 3, 1, 3), 4, 3)                    # drag the fill handle down to row 5
        assert [t.item(r, 3).text() for r in range(1, 5)] == ["44", "44.9", "45.5", "46"], \
            [t.item(r, 3).text() for r in range(1, 5)]
        assert t.item(4, 3).data(de.FORMULA_ROLE) == "=A5+B5"     # references moved with the row
        type_into(1, 1, "15")                           # change an input -> dependants recompute
        assert t.item(1, 3).text() == "45"
        type_into(2, 2, "=$A$2*2")
        dlg._set_cell(4, 4, 1.0)                        # neighbour data for the double-click fill
        t._sel = lambda: (2, 2, 2, 2)
        dlg.fill_down_auto()                            # fill down along column B's data
        assert [t.item(r, 2).text() for r in (2, 3, 4)] == ["60", "60", "60"]
        type_into(3, 0, "=A9/0")
        assert t.item(3, 0).text().startswith("#ERR") and t.item(3, 3).text().startswith("#ERR")   # D4 uses A4
        assert dlg.errors() == 2, dlg.errors()
        name, out = dlg.values()
        assert out[1][3] == 45.0 and out[1][2] == 10.0 and out[3][0] is None and (len(out[3]) < 4 or out[3][3] is None), out
        t.setCurrentCell(4, 0)
        dlg.paste("=A2*10")                             # a pasted formula is a formula too
        assert t.item(4, 0).text() == "300" and t.item(4, 0).data(de.FORMULA_ROLE) == "=A2*10"

    def eye_always_hidden():
        only_current()
        win._history_flush()
        for i in range(win.data_list.count()):              # several rows still selected (e.g. Ctrl+A) ...
            win.data_list.item(i).setSelected(win.data_list.item(i).data(256) in ("3.4GHz", "3.5GHz", "3.6GHz"))
        win.on_eye("3.6GHz")                                  # ... the eye changes ONLY the clicked row
        assert c.muted == ["3.6GHz"], c.muted
        win.on_eye("3.6GHz")
        assert c.muted == []
        for i in range(win.data_list.count()):
            win.data_list.item(i).setSelected(False)
        win.on_eye("3.6GHz")                                  # eye off
        assert c.is_muted("3.6GHz")
        win.on_filter_changed("3.")                           # search + Enter: 3.6 stays off
        win.on_filter_show_only()
        assert "3.6GHz" not in c.checked and "3.4GHz" in c.checked, c.checked
        assert "3.6GHz" not in {l.get_gid() for l in ax().get_lines()}
        win.on_filter_changed("")
        win.set_all_checked(True)                             # 모두 체크: still off
        assert "3.6GHz" not in c.checked
        win.build_data_menu(["3.5GHz", "3.6GHz"])[1]["add"].trigger()      # multi 같이 보기: still off
        assert "3.6GHz" not in [r.sheet_name for r in c.visible_sheets()]
        sim36 = "sim:" + [d for d in c.all_sim_datasets() if abs((d.freq_hz or 0) - 3.6e9) < 1e6][0].name
        win.on_eye(sim36)                                     # a simulation too
        assert sim36[4:] not in {d.name for d in c.visible_sims()}
        win._history_flush()
        win.on_undo()                                         # Ctrl+Z
        assert not c.is_muted(sim36)
        win.on_eye("3.6GHz")                                  # eye on again -> back on the plot
        win.refresh(rerender=True)
        assert not c.is_muted("3.6GHz") and "3.6GHz" in {l.get_gid() for l in ax().get_lines()}
        it = item("3.5GHz")                                   # ticking a hidden one's box = eye on
        win.on_eye("3.5GHz")
        c.set_checked("3.5GHz", True)
        assert not c.is_muted("3.5GHz")
        only_current()

    def eye_fast_clicks_select_nothing():
        """Fast clicks on eyes: the press is the eye's, and so are the move / release / double-click
        of that click -> no rows get (drag-)selected, no data editor opens."""
        from PySide6.QtWidgets import QListWidget
        lst = win.data_list
        calls = []
        orig = {n: getattr(QListWidget, n, None) for n in ("mouseMoveEvent", "mouseReleaseEvent", "mousePressEvent",
                                                          "mouseDoubleClickEvent")}

        class Ev:
            def __init__(self, x, y):
                self._p = (x, y)

            def position(self):
                p = self._p

                class P:
                    def toPoint(self_inner):
                        class Q:
                            def x(self2):
                                return p[0]

                            def y(self2):
                                return p[1]
                        return Q()
                return P()

            def button(self):
                return 1

            def accept(self):
                pass
        rows = [lst.item(i) for i in range(lst.count()) if lst.item(i).data(win.KIND_ROLE) == "meas"][:3]
        opened = []
        orig_hit, orig_at, orig_open = lst.icon_hit, lst.itemAt, lst.on_open
        try:
            for n in orig:
                setattr(QListWidget, n, lambda self, e, n=n: calls.append(n))
            lst.icon_hit = lambda it, pos: True
            lst.on_open = lambda name: opened.append(name)
            for k, it in enumerate(rows + rows[:1]):
                lst.itemAt = lambda pos, it=it: it
                lst.mousePressEvent(Ev(5, 5))
                lst.mouseMoveEvent(Ev(6, 9))           # hand moves a little while pressing
                lst.mouseReleaseEvent(Ev(6, 9))
            lst.mouseDoubleClickEvent(Ev(6, 9))         # quick second click on the same eye
        finally:
            for n, f in orig.items():
                if f is not None:
                    setattr(QListWidget, n, f)
            lst.icon_hit, lst.itemAt, lst.on_open = orig_hit, orig_at, orig_open
        assert calls == [], calls                       # nothing reached Qt's selection handling
        assert opened == []                             # double-click on an eye did not open the editor
        name0 = rows[0].data(256)
        assert c.is_muted(rows[1].data(256)) and c.is_muted(rows[2].data(256))
        assert c.is_muted(name0)                        # toggled 3x (on, off, on via double-click)
        c.set_muted([r.data(256) for r in rows], False)
        win.refresh(rerender=True)

    def key_event(k):
        from PySide6.QtCore import Qt as _Q
        code = {"F2": _Q.Key_F2, "Space": _Q.Key_Space}[k]
        if not _fake():
            from PySide6.QtCore import QEvent
            from PySide6.QtGui import QKeyEvent
            return QKeyEvent(QEvent.KeyPress, code, _Q.NoModifier)

        class E:
            def key(self):
                return code

            def modifiers(self):
                return 0

            def matches(self, *_):
                return False

            def accept(self):
                pass
        return E()

    def f2_space_on_several():
        import ui.main_window as mw
        only_current()
        lst = win.data_list
        three = ["3.4GHz", "3.5GHz", "3.6GHz"]

        def select(names):
            for i in range(lst.count()):
                lst.item(i).setSelected(lst.item(i).data(256) in names)
        select(three)
        lst.keyPressEvent(key_event("Space"))                  # Space: all three on
        assert all(n in [r.sheet_name for r in c.visible_sheets()] for n in three), c.checked
        lst = win.data_list
        select(three)
        lst.keyPressEvent(key_event("Space"))                  # again: all three off
        assert not any(n in c.checked for n in three), c.checked
        orig = mw.QInputDialog.getText
        try:
            mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("F2이름", True))
            lst = win.data_list
            select(three[:2])
            lst.keyPressEvent(key_event("F2"))                 # F2 with two selected -> both renamed
            assert [c.measurement.sheet(n).label for n in three[:2]] == ["F2이름", "F2이름"]
            assert c.measurement.sheet("3.6GHz").label != "F2이름"
            mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("하나만", True))
            lst = win.data_list
            select(["3.6GHz"])
            lst.setCurrentItem(item("3.6GHz"))
            lst.keyPressEvent(key_event("F2"))                 # one selected -> only that one
            assert c.measurement.sheet("3.6GHz").label == "하나만"
            assert c.measurement.sheet("3.4GHz").label == "F2이름"
        finally:
            mw.QInputDialog.getText = orig
        win._history_flush()
        for _ in range(3):
            win.on_undo()
        only_current()

    def paste_remembers_name():
        import ui.main_window as mw
        import ui.paste_dialog as pd
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain"); _x, e = ds.xy("pout", "pae")
        made = []

        class Real(pd.PasteDialog):                            # the real dialog, answered automatically
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                made.append(self)

            def exec(self):
                return True
        orig = mw.PasteDialog
        try:
            mw.PasteDialog = Real
            win.settings.setValue("last_paste_name", "")
            win.paste_text(raw_tsv(x, g, e))
            made[-1].name_edit.setText("LotA_보드3")             # (typed by the user in the 1st paste)
            k1 = c.sheet_name
            mw.PasteDialog = type("Answer", (Real,), {"values": lambda self: ("LotA_보드3", 3.6e9)})
            win.paste_text(raw_tsv(x, g, e))
            k2 = c.sheet_name
            mw.PasteDialog = Real
            win.paste_text(raw_tsv(x, g, e))                   # 3rd paste: the dialog offers the last name
            assert made[-1].name_edit.text() == "LotA_보드3", made[-1].name_edit.text()
            k3 = c.sheet_name
        finally:
            mw.PasteDialog = orig
        for k in (k1, k2, k3):
            c.remove_sheet(k)
        only_current()

    def marker_type_value():
        import ui.main_window as mw
        only_current()
        win.activate_view(win.views[0])
        c.v_markers = [36.0]
        c.cur_h.clear(); c.cur_h.append(40.0)
        win.refresh(rerender=True)
        orig = mw.QInputDialog.getText
        try:
            mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("38.37 dBm", True))
            ev("button_press_event", 36.0, 20.0, button=1, dblclick=True)          # double-click the V marker
            assert c.v_markers == [38.37], c.v_markers                           # exact, not snapped
            yl = ax().get_ylim()
            xl = ax().get_xlim()
            mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("52.25", True))
            ev("button_press_event", xl[0] + 0.15 * (xl[1] - xl[0]), 40.0, button=1, dblclick=True)   # H marker
            assert c.cur_h == [52.25], c.cur_h
            mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("", False))        # Cancel: unchanged
            ev("button_press_event", 38.37, 20.0, button=1, dblclick=True)
            assert c.v_markers == [38.37]
        finally:
            mw.QInputDialog.getText = orig
        c.v_markers = []
        c.cur_h.clear()
        win.refresh(rerender=True)

    def pick_with_search_and_multiselect():
        only_current()
        c.checked = ["3.4GHz", "3.5GHz", "3.6GHz", "FREQ_3P7G"]
        win.on_filter_changed("GHz")
        win.refresh(rerender=True)
        for i in range(win.data_list.count()):                # several rows selected …
            it = win.data_list.item(i)
            it.setSelected(it.data(256) in ("3.4GHz", "3.5GHz"))
        win.on_data_selection()
        win.refresh(rerender=True)
        win.on_plot_pick("3.6GHz")                            # … then a curve is clicked in the plot
        win.refresh(rerender=True)
        ln = {l.get_gid(): l for l in ax().get_lines() if l.get_gid()}
        assert ln["3.6GHz"].get_zorder() == 10, ln["3.6GHz"].get_zorder()     # the clicked one is emphasised
        assert ln["3.4GHz"].get_zorder() < 10 and (ln["3.4GHz"].get_alpha() or 1) < 0.3
        sel = [it.data(256) for it in win.data_list.selectedItems()]
        assert sel == ["3.6GHz"], sel                         # and selected in the list
        win.on_filter_changed("")
        only_current()

    def selected_plot_button():
        only_current()
        c.checked = ["3.4GHz"]
        win.refresh(rerender=True)
        for i in range(win.data_list.count()):
            it = win.data_list.item(i)
            it.setSelected(it.data(256) in ("3.5GHz", "FREQ_3P7G"))
        win.on_selected_show_only()                           # [선택 Plot]
        assert sorted(c.checked) == ["3.5GHz", "FREQ_3P7G"], c.checked
        assert "3.4GHz" not in {l.get_gid() for l in ax().get_lines()}
        for i in range(win.data_list.count()):
            win.data_list.item(i).setSelected(False)
        only_current()

    def sort_and_quick_save():
        import tempfile
        import ui.main_window as mw
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain"); _x, e = ds.xy("pout", "pae")
        keys = []
        for nm, hz in (("정렬보드", 3.7e9), ("다른", 3.5e9), ("정렬보드", 3.4e9), ("정렬보드", 3.6e9)):
            win.paste_text(raw_tsv(x, g, e), ask_freq=False, name=nm, freq_hz=hz)
            keys.append(c.sheet_name)
        win._history_flush()
        win.on_sort_datasets()
        order = [r.sheet_name for r in c.measurement.sheets]
        grp = [k for k in order if c.measurement.sheet(k).label == "정렬보드"]
        assert [c.measurement.sheet(k).dataset.freq_hz for k in grp] == [3.4e9, 3.6e9, 3.7e9], grp
        assert order.index(grp[0]) < order.index(keys[1]) and order.index(grp[-1]) < order.index(keys[1])
        win._history_flush()
        win.on_undo()                                         # Ctrl+Z: previous order back
        assert [r.sheet_name for r in c.measurement.sheets] != order
        path = os.path.join(tempfile.mkdtemp(), "quick.pam")
        asked = []
        old_path = c.measurement.path
        c.measurement.path = "not_a_project.xlsx"           # no project open yet
        orig = mw.QFileDialog.getSaveFileName
        try:
            mw.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (asked.append(1), (path, ""))[1])
            assert win.on_save_project_quick() == path and len(asked) == 1      # first time: asks where
            c.rename_sheet(keys[1], "덮어쓰기확인")
            assert win.on_save_project_quick() == path and len(asked) == 1      # Ctrl+S: overwrites, no dialog
        finally:
            mw.QFileDialog.getSaveFileName = orig
        from measurement.project import load_project
        assert any(r.label == "덮어쓰기확인" for r in load_project(path).sheets)
        c.measurement.path = old_path
        for k in keys:
            c.remove_sheet(k)
        only_current()

    def plot_right_click_menu():
        only_current()
        c.checked = ["3.4GHz", "3.6GHz"]
        win.refresh(rerender=True)
        got = []
        orig = win.on_plot_context
        win.views[0]._on_context = lambda what: got.append(what)
        try:
            xl, yl = ax().get_xlim(), ax().get_ylim()
            x, y = xl[0] + 0.03 * (xl[1] - xl[0]), yl[1] - 0.04 * (yl[1] - yl[0])
            for nm in ("button_press_event", "button_release_event"):   # right-click, no drag
                ev(nm, x, y, button=3)
            assert got == [("plot", None)], got
            assert ax().get_xlim() == xl                                  # no zoom
            got.clear()
            line = [l for l in ax().get_lines() if l.get_gid() == "3.6GHz"][0]
            xd, yd = line.get_data(); k = len(xd) // 2
            ev("button_press_event", float(xd[k]), float(yd[k]), button=3)
            ev("button_release_event", float(xd[k]), float(yd[k]), button=3)
            assert got and got[0][0] == "plot" and got[0][1] in ("3.6GHz", "3.4GHz"), got
            got.clear()
            ev("button_press_event", x, y, button=3)                      # right-DRAG = box zoom (as before)
            ev("button_release_event", xl[0] + 0.5 * (xl[1] - xl[0]), yl[1] - 0.5 * (yl[1] - yl[0]), button=3)
            assert not got and ax().get_xlim() != xl
        finally:
            win.views[0]._on_context = orig
        win.on_reset()
        menu, acts = win.build_plot_menu("3.6GHz")
        for k in ("curve", "open", "all", "none", "selected", "mk:v", "fit", "reset_all", "distinct", "copy", "png",
                  "excel", "show:3.4GHz", "graph:" + c.graph_key):
            assert k in acts, k
        assert acts["show:3.6GHz"].isChecked() and not acts["show:FREQ_3P7G"].isChecked(), (c.checked, acts["show:3.6GHz"]._props)
        acts["show:FREQ_3P7G"].triggered.emit(True)                      # Datasets 표시 → add one
        assert "FREQ_3P7G" in c.checked
        acts["show:3.4GHz"].triggered.emit(False)
        assert "3.4GHz" not in c.checked
        other = next(g.key for g in c.graphs if g.key != c.graph_key)
        acts["graph:" + other].triggered.emit(True)                      # 그래프 종류
        assert c.graph_key == other
        acts["mk:v"].triggered.emit(False)
        assert c.v_markers
        win.on_clear_markers()
        win.graph_buttons[c.graphs[0].key].setChecked(True)
        only_current()

    def meas_gids():
        return sorted(l.get_gid() for l in ax().get_lines() if l.get_gid() and not str(l.get_gid()).startswith("sim:"))

    def select_rows(names):
        for i in range(win.data_list.count()):
            win.data_list.item(i).setSelected(win.data_list.item(i).data(256) in names)

    def uncheck_all_then_browse_and_space():
        win.set_all_checked(False)                                  # [모두 해제] = really nothing
        assert meas_gids() == [], meas_gids()
        win.data_list.setCurrentItem(item("3.4GHz"))                # click a row -> previewed
        win.refresh(rerender=True)
        assert meas_gids() == ["3.4GHz"], meas_gids()
        assert item("3.4GHz").checkState() == Qt().Unchecked        # (box stays empty: only a preview)
        select_rows(["3.4GHz"])
        win.data_list.keyPressEvent(key_event("Space"))            # Space -> pinned
        win.refresh(rerender=True)
        assert c.browse_name is None and item("3.4GHz").checkState() == Qt().Checked, (c.checked, c.browse_name)
        win.data_list.setCurrentItem(item("3.6GHz"))                # browse on: pinned one stays
        win.refresh(rerender=True)
        assert meas_gids() == ["3.4GHz", "3.6GHz"], meas_gids()
        win.data_list.setCurrentItem(item("FREQ_3P7G"))
        win.refresh(rerender=True)
        assert meas_gids() == ["3.4GHz", "FREQ_3P7G"], meas_gids()
        select_rows(["3.4GHz"])
        win.data_list.setCurrentItem(item("3.4GHz"))
        win.data_list.keyPressEvent(key_event("Space"))            # Space again -> off
        win.refresh(rerender=True)
        assert "3.4GHz" not in meas_gids(), meas_gids()
        select_rows([])
        only_current()

    def selected_plot_simulations():
        only_current()
        sims = [win.data_list.item(i).data(256) for i in range(win.data_list.count())
                if win.data_list.item(i).data(257) == "sim"]
        two = sims[:2]
        select_rows(two)
        win.on_selected_show_only()                                # [선택 Plot] with simulations only
        sim_lines = sorted(l.get_gid() for l in ax().get_lines() if str(l.get_gid()).startswith("sim:"))
        assert meas_gids() == [] and sim_lines == sorted(two), (meas_gids(), sim_lines)
        menu, acts = win.build_data_menu(sims[2:4])                 # right-click on simulations: 이것들만 보기
        assert "only" in acts
        acts["only"].triggered.emit(False)
        sim_lines = sorted(l.get_gid() for l in ax().get_lines() if str(l.get_gid()).startswith("sim:"))
        assert sim_lines == sorted(sims[2:4]), sim_lines
        select_rows(["3.6GHz", sims[0]])                            # measurement + simulation together
        win.on_selected_show_only()
        sim_lines = sorted(l.get_gid() for l in ax().get_lines() if str(l.get_gid()).startswith("sim:"))
        assert meas_gids() == ["3.6GHz"] and sim_lines == [sims[0]], (meas_gids(), sim_lines)
        select_rows([])
        win.set_all_checked(True)
        only_current()

    def multi_highlight_all_shown():
        three = ["3.4GHz", "3.6GHz", "FREQ_3P7G"]
        c.checked = list(three)
        win.refresh(rerender=True)
        select_rows(three)                                          # ALL plotted curves selected
        win.on_data_selection()
        win.refresh(rerender=True)
        z = {l.get_gid(): l.get_zorder() for l in ax().get_lines() if l.get_gid() in three}
        assert all(v == 10 for v in z.values()) and len(z) == 3, z
        select_rows([])
        win.on_data_clear()
        only_current()

    def fav_shift_select_and_check_zone():
        lst = win.data_list
        favs = ["3.4GHz", "3.6GHz", "FREQ_3P7G"]
        c.add_favorites(favs)
        win._fill_data_list()
        def row(name, kind):
            return next(lst.item(i) for i in range(lst.count())
                        if lst.item(i).data(256) == name and lst.item(i).data(257) == kind)
        def sel():
            return sorted((it.data(256), it.data(257)) for it in lst.selectedItems())
        for it in lst.selectedItems():
            it.setSelected(False)
        lst._anchor = ("3.4GHz", "fav")                              # clicked the first ★ row,
        assert lst.shift_select(row("FREQ_3P7G", "fav"))             # then Shift+click the last ★ row
        assert sel() == sorted((n, "fav") for n in favs), sel()      # only ★ rows (no Measurement rows)
        win.on_data_selection()
        win.refresh(rerender=True)
        assert sel() == sorted((n, "fav") for n in favs), sel()      # still only ★ rows after the list refresh
        assert sorted(c.highlight_multi) == sorted(favs)
        lst._anchor = ("3.4GHz", "meas")                             # anchor in Measurement, Shift on a ★ row
        lst.shift_select(row("3.6GHz", "fav"))
        assert sel() == [("3.4GHz", "fav"), ("3.6GHz", "fav")], sel()
        for it in lst.selectedItems():
            it.setSelected(False)
        win.on_data_clear()
        # wide check-box area: a click there ticks / unticks the row (no selection change)
        class Ev:
            def position(self):
                class P:
                    def toPoint(self_inner):
                        return None
                return P()
            def accept(self):
                pass
        c.checked = ["3.4GHz"]
        win.refresh(rerender=True)
        target = row("3.6GHz", "meas")
        orig_at, orig_chk, orig_hit = lst.itemAt, lst._is_check, lst.icon_hit
        try:
            lst.itemAt = lambda pos: target
            lst._is_check = lambda it, pos: True
            lst.icon_hit = lambda it, pos: False
            lst.mousePressEvent(Ev())
            lst.mouseReleaseEvent(Ev())
            assert "3.6GHz" in c.checked, c.checked
            assert lst.selectedItems() == []
            lst.mouseDoubleClickEvent(Ev())                         # quick second click = untick again
            assert "3.6GHz" not in c.checked
        finally:
            lst.itemAt, lst._is_check, lst.icon_hit = orig_at, orig_chk, orig_hit
        c.remove_favorites(favs)
        only_current()

    def own_plots_3_4_and_delete():
        import tempfile
        from measurement.project import load_project
        n_old = win.n_panes
        win.set_layout(4)
        c.checked = ["3.4GHz", "3.6GHz"]
        win.refresh(rerender=True)
        def gids(i):
            v = win.views[i]
            v.render()
            return sorted(l.get_gid() for l in v.renderer.ax.get_lines()
                          if l.get_gid() and not str(l.get_gid()).startswith("sim:"))
        assert gids(0) == ["3.4GHz", "3.6GHz"] and gids(2) == [] and gids(3) == []   # Plot 3 / 4 start empty
        assert any("끌어다" in t.get_text() for t in win.views[2].renderer.ax.texts)
        select_rows(["FREQ_3P7G"])
        class Drop:                                                  # drag from the Datasets list onto Plot 3
            def source(self):
                return win.data_list
            def acceptProposedAction(self):
                pass
            def ignore(self):
                pass
        win.views[2].dropEvent(Drop())
        assert gids(2) == ["FREQ_3P7G"] and gids(0) == ["3.4GHz", "3.6GHz"], (gids(2), gids(0))
        assert c.active_pane == 2
        item("3.4GHz").setCheckState(Qt().Checked)                  # Plot 3 active: the tick is for Plot 3 only
        win.refresh(rerender=True)
        assert gids(2) == ["3.4GHz", "FREQ_3P7G"] and gids(3) == [] and gids(0) == ["3.4GHz", "3.6GHz"]
        win.on_drop_to_view(win.views[3], names=["3.6GHz"])
        assert gids(3) == ["3.6GHz"]
        win.activate_view(win.views[2])                              # Delete on a curve picked in Plot 3
        win.on_plot_pick("FREQ_3P7G")
        win.on_plot_delete(win.views[2])
        assert gids(2) == ["3.4GHz"] and c.measurement.sheet("FREQ_3P7G") is not None
        win.activate_view(win.views[0])                              # Plot 1: same, the data stays
        win.on_plot_pick("3.6GHz")
        win.on_plot_delete(win.views[0])
        assert gids(0) == ["3.4GHz"] and c.measurement.sheet("3.6GHz") is not None
        win.on_plot_pick("3.4GHz")
        win.on_plot_delete(win.views[0])                             # last one off -> really empty
        assert gids(0) == [], gids(0)
        path = win.on_save_project(path=os.path.join(tempfile.mkdtemp(), "own.pam"))
        st = load_project(path).settings
        assert st["panes"][2]["own"] == ["3.4GHz"] and st["panes"][3]["own"] == ["3.6GHz"] and st["panes"][0]["own"] is None
        win._history_flush()
        win.on_undo()
        assert gids(0) == ["3.4GHz"], gids(0)
        for i in (2, 3):
            c.panes[i].own_meas = []
        win.activate_view(win.views[0])
        select_rows([])
        win.set_layout(n_old)
        only_current()

    def plot_ctrl_click_multi():
        three = ["3.4GHz", "3.6GHz", "FREQ_3P7G"]
        c.checked = list(three)
        win.refresh(rerender=True)
        def click_curve(gid, **kw):
            line = [l for l in ax().get_lines() if l.get_gid() == gid][0]
            xd, yd = line.get_data()
            others = [l for l in ax().get_lines() if l.get_gid() and l is not line]
            import numpy as _np
            def gap(i):
                p = ax().transData.transform((xd[i], yd[i]))
                return min(_np.min(_np.hypot(*(ax().transData.transform(_np.column_stack(o.get_data())) - p).T))
                           for o in others)
            x0, x1 = ax().get_xlim(); y0, y1 = ax().get_ylim()
            k = max([i for i in range(len(xd)) if x0 < xd[i] < x1 and y0 < yd[i] < y1], key=gap)
            for nm in ("button_press_event", "button_release_event"):
                ev(nm, float(xd[k]), float(yd[k]), button=1, **kw)
            win.refresh(rerender=True)
        click_curve("3.4GHz")
        click_curve("3.6GHz", key="control")                        # Ctrl+click: add
        assert sorted(c.highlight_multi) == ["3.4GHz", "3.6GHz"], c.highlight_multi
        z = {l.get_gid(): l.get_zorder() for l in ax().get_lines() if l.get_gid() in three}
        assert z["3.4GHz"] == 10 and z["3.6GHz"] == 10 and z["FREQ_3P7G"] != 10, z
        sel = {it.data(256) for it in win.data_list.selectedItems()}
        assert sel == {"3.4GHz", "3.6GHz"}, sel                       # list shows the same choice
        click_curve("FREQ_3P7G", key="shift")                       # Shift+click: add
        assert len(c.highlight_multi) == 3
        xl, yl = ax().get_xlim(), ax().get_ylim()
        for k in ("control", "shift"):                              # Ctrl / Shift + empty space: kept
            for nm in ("button_press_event", "button_release_event"):
                ev(nm, xl[0] + 0.02 * (xl[1] - xl[0]), yl[1] - 0.03 * (yl[1] - yl[0]), button=1, key=k)
            win.refresh(rerender=True)
            assert len(c.highlight_multi) == 3, (k, c.highlight_multi)
            assert len({it.data(256) for it in win.data_list.selectedItems()}) == 3
        click_curve("3.6GHz", key="control")                        # again: take it out
        assert sorted(c.highlight_multi) == ["3.4GHz", "FREQ_3P7G"]
        click_curve("3.6GHz")                                       # plain click: just that one
        assert c.highlight_multi == [] and c.sheet_name == "3.6GHz"
        win.on_plot_pick(None)
        only_current()

    def plot_and_axis_titles():
        import tempfile
        from measurement.project import load_project
        c.checked = ["3.4GHz"]
        win.refresh(rerender=True)
        d = c.default_labels()
        win.on_edit_labels(win.views[0], values={"title": "내 Plot 제목", "x": "Pin (dBm)", "y": ""})
        win.views[0].render()
        assert ax().get_xlabel() == "Pin (dBm)" and ax().get_ylabel() == d["y"]
        assert any(t.get_text() == "내 Plot 제목" for t in win.figure.texts)
        with c.using_pane(1):                                       # other plots keep their own names
            assert c.plot_labels()["x"] == c.default_labels()["x"]
        path = win.on_save_project(path=os.path.join(tempfile.mkdtemp(), "labels.pam"))
        st = load_project(path).settings
        assert st["panes"][0]["labels"][c.graph_key]["x"] == "Pin (dBm)"
        got = []
        orig = win.on_edit_labels
        win.on_edit_labels = lambda v=None, part="title", values=None: got.append(part)
        try:
            win.canvas.draw()
            bb = ax().xaxis.label.get_window_extent(win.canvas.get_renderer())
            from matplotlib.backend_bases import MouseEvent
            e = MouseEvent("button_press_event", win.canvas, (bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2, button=1,
                           dblclick=True)
            win.canvas.callbacks.process("button_press_event", e)      # double-click on the X-axis title
            assert got == ["x"], got
        finally:
            win.on_edit_labels = orig
        menu, acts = win.build_plot_menu(None)
        assert "labels" in acts
        win.on_edit_labels(win.views[0], values={"title": "", "x": "", "y": ""})   # empty = default again
        assert c.plot_labels() == c.default_labels()
        only_current()

    def paste_uses_mapping_favourite():
        from measurement import mapping_presets
        from ui.paste_dialog import PasteDialog
        old_p, old_l = win.settings.value(mapping_presets.KEY, ""), win.settings.value("mapping_last", "")
        odd = "aa\tbb\tcc\r\n" + "".join(f"{30 + i}\t{14 - 0.1 * i}\t{20 + 2 * i}\r\n" for i in range(8))
        win.paste_text(odd, ask_freq=False, name="이상한헤더", freq_hz=3.5e9)     # not recognised (no favourite yet)
        first = c.sheet_name
        assert not c.measurement.sheet(first).ok
        p = mapping_presets.make_preset("맵핑Z", c.measurement.sheet(first), 0, {"pout": 0, "gain": 1, "pae": 2})
        mapping_presets.save(win.settings, [p])
        win.settings.setValue("mapping_last", "맵핑Z")
        win.paste_text(odd, ask_freq=False, name="이상한헤더2", freq_hz=3.5e9)    # now: the favourite is used
        r = c.measurement.sheet(c.sheet_name)
        assert r.ok and r.user_mapping, r.messages
        x, g = r.dataset.xy("pout", "gain")
        assert abs(x[0] - 30) < 1e-9 and abs(g[0] - 14) < 1e-9
        win.paste_text(odd, ask_freq=False, name="이상한헤더3", freq_hz=3.5e9)
        k = c.sheet_name                                   # paste window '맵핑' choice: automatic <-> favourite
        dlg = PasteDialog(c.measurement.sheet(k), win, presets=["맵핑Z"], preset="맵핑Z",
                          on_preset=lambda nm: win._paste_preset(k, nm))
        dlg.choose_preset(None)
        assert not c.measurement.sheet(k).ok and not dlg.result.ok
        dlg.choose_preset("맵핑Z")
        from ui.paste_dialog import describe
        assert c.measurement.sheet(k).ok and dlg.result.ok and "인식한 열" in describe(dlg.result)
        for n in (first, k, r.sheet_name):
            c.remove_sheet(n)
        win.settings.setValue(mapping_presets.KEY, old_p)
        win.settings.setValue("mapping_last", old_l)
        only_current()

    def line_favourite_buttons():
        from ui import line_favs
        for i in (1, 2):
            line_favs.reset(win.settings, i)
        c.checked = ["3.4GHz", "3.6GHz"]
        win.refresh(rerender=True)
        select_rows(["3.4GHz"])
        win.on_line_fav(1)
        assert {k: c.style_of_dataset("3.4GHz").get(k) for k in ("linestyle", "linewidth", "marker")} == \
            {"linestyle": "--", "linewidth": 1.2, "marker": ""}
        select_rows(["3.6GHz"])
        win.on_line_fav(2)
        st = c.style_of_dataset("3.6GHz")
        assert st["linestyle"] == "-" and st["linewidth"] == 2.4 and st["marker"] == ""
        ln = {l.get_gid(): l for l in ax().get_lines() if l.get_gid() in ("3.4GHz", "3.6GHz")}
        assert ln["3.4GHz"].get_linestyle() == "--" and ln["3.6GHz"].get_marker() in ("None", "", None)
        menu, acts = win.build_line_fav_menu(1)                    # right-click 선1: change its look
        acts["linewidth:2.4"].triggered.emit(True)
        assert line_favs.load(win.settings, 1)["linewidth"] == 2.4 and "굵은" in line_favs.describe(line_favs.load(win.settings, 1))
        acts["reset"].triggered.emit(False)
        assert line_favs.load(win.settings, 1) == line_favs.DEFAULTS[1]
        menu, acts = win.build_data_menu(["3.4GHz"])                # also in the Datasets right-click menu
        assert "line_fav:1" in acts and "line_fav:2" in acts
        select_rows([])
        win.on_lines_default()
        only_current()

    def dataset_info_menu():
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain"); _x, e = ds.xy("pout", "pae")
        win.paste_text(raw_tsv(x, g, e), ask_freq=False, name="정보보기", freq_hz=3.55e9)
        k = c.sheet_name
        import datetime as _dt
        menu, acts = win.build_data_menu([k])
        assert "info" in acts
        text = win.on_dataset_info([k])
        assert _dt.date.today().isoformat() in text and "붙여넣기" in text and "3.55" in text, text
        c.measurement.set_frequency(k, 3.56e9)
        text = win.on_dataset_info([k])
        assert "마지막 수정" in text and _dt.date.today().isoformat() in text.split("마지막 수정")[1]
        assert "추가한 때" in text and "붙여넣기" in text                    # kept through the change
        text = win.on_dataset_info([k, "3.4GHz"])
        assert "선택한 2개" in text
        import tempfile
        from measurement.project import load_project
        path = win.on_save_project(path=os.path.join(tempfile.mkdtemp(), "info.pam"))
        r2 = load_project(path).sheet(k)
        assert r2.added_at == c.measurement.sheet(k).added_at and r2.added_how.startswith("붙여넣기")
        c.remove_sheet(k)
        only_current()

    def favourites_section():
        lst = win.data_list
        def rows(kind):
            return [lst.item(i) for i in range(lst.count()) if lst.item(i).data(257) == kind]
        win._fill_data_list()
        heads = [lst.item(i).data(257) for i in range(lst.count()) if str(lst.item(i).data(257)).startswith("header:")]
        assert heads[:2] == ["header:meas", "header:fav"], heads        # between Measurement and Simulation
        fav_head = next(i for i in range(lst.count()) if lst.item(i).data(257) == "header:fav")
        select_rows(["3.4GHz", "3.6GHz"])                             # drag two Measurement rows onto ★
        assert lst.drop_rows(["3.4GHz", "3.6GHz"], ["meas", "meas"], fav_head + 1)
        win._fill_data_list()
        assert [it.data(256) for it in rows("fav")] == ["3.4GHz", "3.6GHz"]
        assert c.measurement.sheet("3.4GHz") is not None
        fav_rows_start = next(i for i in range(lst.count()) if lst.item(i).data(257) == "fav")
        assert lst.drop_rows(["3.6GHz"], ["fav"], fav_rows_start)     # reorder inside ★
        win._fill_data_list()
        assert [it.data(256) for it in rows("fav")] == ["3.6GHz", "3.4GHz"]
        sim_head = next(i for i in range(lst.count()) if lst.item(i).data(257) == "header:sim")
        assert lst.drop_rows(["FREQ_3P7G"], ["meas"], sim_head + 1)    # onto Simulation: nothing happens
        assert "FREQ_3P7G" not in c.favorite_names()
        win.filter_edit.setText("zzz_없음")                            # search: ★ rows stay visible
        win.on_filter_changed("zzz_없음")
        assert all(not it.isHidden() for it in rows("fav")) and all(it.isHidden() for it in rows("meas"))
        fav_items = rows("fav")
        fav_items[0].setCheckState(Qt().Checked)                      # ticking a ★ row = that measurement
        assert "3.6GHz" in c.checked
        win.filter_edit.setText(""); win.on_filter_changed("")
        for it in lst.selectedItems():
            it.setSelected(False)
        rows("fav")[0].setSelected(True)                              # Delete on a ★ row: only out of ★
        win.on_delete_datasets(confirm=False)
        win._fill_data_list()
        assert [it.data(256) for it in rows("fav")] == ["3.4GHz"] and c.measurement.sheet("3.6GHz") is not None
        menu, acts = win.build_data_menu(["3.4GHz"], from_fav=True)
        assert "fav_remove" in acts and "★" in acts["delete"].text()
        acts["delete"].triggered.emit(False)
        assert c.favorite_names() == [] and c.measurement.sheet("3.4GHz") is not None
        menu, acts = win.build_data_menu(["FREQ_3P7G"])                # right-click → ★ 즐겨찾기에 추가
        acts["fav_add"].triggered.emit(False)
        assert c.favorite_names() == ["FREQ_3P7G"]
        import tempfile
        from measurement.project import load_project
        path = win.on_save_project(path=os.path.join(tempfile.mkdtemp(), "fav.pam"))
        assert load_project(path).settings.get("favorites") == ["FREQ_3P7G"]
        win._history_flush()
        c.remove_favorites(["FREQ_3P7G"]); win._history_note(); win._history_flush()
        win.on_undo()
        assert c.favorite_names() == ["FREQ_3P7G"]
        c.remove_favorites(["FREQ_3P7G"])
        for it in lst.selectedItems():
            it.setSelected(False)
        only_current()

    def mapping_favourites():
        from measurement import mapping_presets
        from ui.column_mapping_dialog import ColumnMappingDialog
        old = win.settings.value(mapping_presets.KEY, "")
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain"); _x, e = ds.xy("pout", "pae")
        keys = []
        for nm in ("맵핑A", "맵핑B"):
            win.paste_text(raw_tsv(x, g, e), ask_freq=False, name=nm, freq_hz=3.5e9)
            keys.append(c.sheet_name)
        r = c.measurement.sheet(keys[0])
        m = dict(r.mapping); m["gain"] = RAW_HEADER.index("DE(%)")       # a deliberately different mapping
        dlg = ColumnMappingDialog(r, win, settings=win.settings)
        dlg.header_spin.setValue(r.header_row + 1)
        for q, cb in dlg.combos.items():
            cb.setCurrentIndex(max(cb.findData(m.get(q, -1)), 0))
        dlg.save_preset("맵핑1")                                           # [현재 맵핑 저장…]
        assert [p["name"] for p in mapping_presets.load(win.settings)] == ["맵핑1"]
        assert dlg.preset_combo.count() == 1
        menu, acts = win.build_data_menu(keys)                             # right-click → 맵핑 즐겨찾기 적용 → 맵핑1
        assert "preset:맵핑1" in acts
        win.apply_mapping_preset("맵핑1", keys)
        for k in keys:
            _x2, g2 = c.measurement.sheet(k).dataset.xy("pout", "gain")
            assert abs(g2[0] - (e[0] + 3)) < 0.01, (g2[0], e[0])           # gain now = DE column
        dlg2 = ColumnMappingDialog(c.measurement.sheet(keys[1]), win, settings=win.settings)
        dlg2.load_preset("맵핑1")                                          # [불러오기] in the dialog
        assert dlg2.values()[1].get("gain") == RAW_HEADER.index("DE(%)")
        win.settings.setValue(mapping_presets.KEY, old)
        for k in keys:
            c.remove_sheet(k)
        only_current()

    def lines_default_button():
        c.checked = ["3.4GHz", "3.6GHz"]
        c.set_styles(["3.4GHz"], color="#000000", linewidth=2.4, linestyle="--", marker="s")
        win.refresh(rerender=True)
        win.on_lines_default()
        st = c.style_of_dataset("3.4GHz")
        assert st == {"color": "#000000"}, st                  # colour kept, line back to default
        c.reset_dataset_style("3.4GHz")
        only_current()

    def undo_redo():
        only_current()
        win._history_flush()
        c.checked = ["3.4GHz", "3.6GHz"]; win.refresh(rerender=True); win._history_flush()   # 1. show two
        s1 = (list(c.checked),)
        c.set_styles(["3.6GHz"], color="#000000"); win.refresh(rerender=True); win._history_flush()   # 2. colour
        c.rename_sheet("3.4GHz", "보드X"); win.refresh(rerender=True); win._history_flush()  # 3. rename
        n_before = len(c.measurement.sheets)
        c.remove_sheet("3.4GHz"); win.refresh(rerender=True); win._history_flush()  # 4. delete
        c.v_markers = [40.0]; win.on_view_changed("markers"); win._history_flush()   # 5. marker
        c.set_ranges((30.0, 45.0), (0.0, 80.0)); win.render(); win._history_flush()   # 6. zoom
        win.on_undo()                                                            # zoom undone
        assert c.ranges().xlim != (30.0, 45.0), c.ranges()
        win.on_undo()                                                            # marker undone
        assert c.v_markers == [], c.v_markers
        win.on_undo()                                                            # delete undone
        assert c.measurement.sheet("3.4GHz") is not None and len(c.measurement.sheets) == n_before
        assert "3.4GHz" in c.checked
        assert "3.4GHz" in {l.get_gid() for l in ax().get_lines()}               # back on the plot
        win.on_undo()                                                            # rename undone
        assert c.measurement.sheet("3.4GHz").label != "보드X"
        win.on_undo()                                                            # colour undone
        assert not c.style_of_dataset("3.6GHz").get("color")
        win.on_redo(); win.on_redo()                                             # colour + rename again
        assert c.style_of_dataset("3.6GHz").get("color") == "#000000"
        assert c.measurement.sheet("3.4GHz").label == "보드X"
        win.on_undo(); win.on_undo()
        assert (list(c.checked),) == s1 and not c.style_of_dataset("3.6GHz").get("color")
        c.set_active_pane(0)                                                     # selecting is not an undo step
        n = len(win.history.undo_stack)
        win.data_list.setCurrentItem(item("3.6GHz")); win.refresh(rerender=True); win._history_flush()
        win.data_list.setCurrentItem(item("3.4GHz")); win.refresh(rerender=True); win._history_flush()
        assert len(win.history.undo_stack) == n, (len(win.history.undo_stack), n)
        only_current()

    def axis_reset_button():
        win.set_layout(2)
        for v in win.visible_views:
            with c.using_pane(v.idx):
                c.set_ranges((33.0, 41.0), (5.0, 20.0))
        win.render()
        win.axis_reset_btn.click() if not _fake() else win.on_reset_all_axes()
        for v in win.visible_views:
            with c.using_pane(v.idx):
                assert c.ranges() == c.default_ranges(), c.ranges()

    def search_keeps_sections_open():
        win.on_filter_changed("")
        hdr = next(win.data_list.item(i) for i in range(win.data_list.count())
                   if win.data_list.item(i).data(win.KIND_ROLE) == "header:sim")
        win.set_list_folded("sim", True)                                         # folded earlier
        win.on_filter_changed("3.6")                                             # searching -> results shown anyway
        sims = [win.data_list.item(i) for i in range(win.data_list.count())
                if win.data_list.item(i).data(win.KIND_ROLE) == "sim" and not win.data_list.item(i).isHidden()]
        assert sims, "search results hidden by a folded section"
        hdr = next(win.data_list.item(i) for i in range(win.data_list.count())
                   if win.data_list.item(i).data(win.KIND_ROLE) == "header:sim")
        win.on_data_clicked(hdr)                                                 # header click while searching: no fold
        assert win._list_folded("sim") is True                                   # (stored state unchanged)
        win.on_filter_changed("")
        hdr = next(win.data_list.item(i) for i in range(win.data_list.count())
                   if win.data_list.item(i).data(win.KIND_ROLE) == "header:sim")
        assert "접힘" in hdr.text()                                              # folded section says so
        win.set_list_folded("sim", False)

    def excel_export():
        import tempfile
        from openpyxl import load_workbook
        c.checked = ["3.4GHz", "3.6GHz", "FREQ_3P7G"]
        c.sheet_name = "3.6GHz"
        win.refresh(rerender=True)
        path = win.export_excel(os.path.join(tempfile.mkdtemp(), "plots"))
        assert path.endswith(".xlsx") and os.path.isfile(path)
        wb = load_workbook(path)
        plot_sheets = [ws for ws in wb.worksheets if ws.title != "Info"]
        assert len(plot_sheets) == win.n_panes and "Info" in wb.sheetnames, wb.sheetnames
        ws = plot_sheets[0]
        charts = ws._charts
        assert len(charts) == 1
        ch = charts[0]
        lines = [l for l in win.views[0].renderer.ax.get_lines() if l.get_gid()]
        assert len(ch.series) == len(lines), (len(ch.series), len(lines))
        xl = win.views[0].renderer.ax.get_xlim()
        st = ch.x_axis.majorUnit
        assert st and ch.x_axis.scaling.min <= xl[0] + 1e-6 and ch.x_axis.scaling.max >= xl[1] - 1e-6
        assert xl[0] - ch.x_axis.scaling.min < st and ch.x_axis.scaling.max - xl[1] < st     # same range, whole ticks
        from plotting.excel_export import DATA_ROW
        x0 = ws.cell(row=DATA_ROW + 2, column=1).value
        assert isinstance(x0, (int, float)) and ws.cell(row=DATA_ROW, column=1).value   # numbers + curve name
        first = lines[0]
        col = 1 + 2 * [l.get_gid() for l in lines].index(first.get_gid())
        assert abs(ws.cell(row=DATA_ROW + 2, column=col).value - float(first.get_xdata()[0])) < 1e-3
        assert (c.highlight_selected is True)                      # emphasis setting untouched

    def rename_in_list():
        import ui.main_window as mw
        orig = mw.QInputDialog.getText
        try:
            mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("Board-A", True))
            win.data_list._rename(item("3.5GHz"))            # = double-click / F2 on the item
            win.refresh(rerender=True)
            assert c.measurement.sheet("3.5GHz").label == "Board-A", c.measurement.sheet("3.5GHz").label
            assert "Board-A" in item("3.5GHz").text()
            mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("", False))   # Cancel -> unchanged
            win.data_list._rename(item("3.5GHz"))
            assert c.measurement.sheet("3.5GHz").label == "Board-A"
            sim_item = next(win.data_list.item(i) for i in range(win.data_list.count())
                            if win.data_list.item(i).data(win.KIND_ROLE) == "sim")
            assert not win.data_list._rename(sim_item)        # simulation rows are not renamed
            mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("3.5GHz", True))
            win.on_rename_dataset("3.5GHz")
            win.refresh(rerender=True)
        finally:
            mw.QInputDialog.getText = orig

    def reorder_and_delete():
        order = win.data_list.item_names("meas")
        new = [order[-1]] + order[:-1]                     # drag the last dataset to the top
        win.on_data_reordered(new)
        assert [r.sheet_name for r in c.measurement.sheets] == new and win.data_list.item_names("meas") == new
        win.on_data_reordered(order)                       # and back
        assert win.data_list.item_names("meas") == order
        # paste two throw-away datasets, select both, delete
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain"); _x, e = ds.xy("pout", "pae")
        keys = []
        for nm in ("tmpA", "tmpB"):
            win.paste_text(raw_tsv(x, g, e), ask_freq=False, name=nm, freq_hz=3.6e9)
            keys.append(c.sheet_name)
        for i in win.data_list.selectedItems():
            i.setSelected(False)
        for k in keys:
            item(k).setSelected(True)
        if _fake():
            win.data_list.setFocus()
            win.on_delete_key()                            # Delete key in the list (confirm answered Yes)
            win.data_list.clearFocus()
        else:
            win.on_delete_datasets(confirm=False)
        assert not any(c.measurement.sheet(k) for k in keys), [r.sheet_name for r in c.measurement.sheets]
        assert win.data_list.item_names("meas") == order

    def multi_select_measurements():
        only_current()
        win.data_list.setCurrentItem(item("3.6GHz"))
        checked_before = list(c.checked)
        from PySide6.QtWidgets import QApplication
        if _fake():
            QApplication.modifiers = Qt().ControlModifier
        for n in ("3.4GHz", "3.5GHz"):                 # Ctrl+click two more measurement items
            it = item(n)
            it.setSelected(True)
            if _fake():                                # fake Qt: Ctrl held -> current changes, selection stays
                win.data_list.setCurrentItem(it, 0)
        if _fake():
            QApplication.modifiers = 0
        win.refresh(rerender=True)                       # a list rebuild must keep the selection
        sel = sorted(i.data(256) for i in win.data_list.selectedItems())
        assert sel == ["3.4GHz", "3.5GHz", "3.6GHz"], sel
        assert c.checked == checked_before, c.checked     # Ctrl+click did not switch the plot
        menu, acts = win.build_data_menu(sel)
        acts["color:#000000"].trigger()
        assert all(c.style_of_dataset(n).get("color") == "#000000" for n in sel)
        acts["reset"].trigger()
        for i in win.data_list.selectedItems():
            i.setSelected(False)

    def mixed_selection_style():
        names = ["3.6GHz", "sim:Typical @ 3.60 GHz"]
        menu, acts = win.build_data_menu(names)
        assert "delete" in acts and "rename" not in acts
        acts["color:#000000"].trigger()
        acts["width:thick"].trigger()
        cols = {l.get_label(): (l.get_color(), l.get_linewidth()) for l in ax().get_lines() if not l.get_label().startswith("_")}
        k = lw_scale()
        assert cols["Measurement"][0] == "#000000" and cols["Simulation (Typical)"][0] == "#000000", cols
        assert abs(cols["Measurement"][1] - 2.4 * k) < 1e-6 and abs(cols["Simulation (Typical)"][1] - 2.4 * k) < 1e-6, cols
        acts["reset"].trigger()

    def header_alignment_and_gan_bias():
        block = ("Freq\tVG_DRV\tVG_CA\tVG_PK\tIQ_DRV\tIQ_CA\r\n3.6\t1.10\t1.20\t-1.35\t54.5\t118.0\r\n\r\n")
        ds = c.measurement.sheet("3.6GHz").dataset
        x, g = ds.xy("pout", "gain")
        _x, e = ds.xy("pout", "pae")
        win.paste_text(block + raw_tsv(x, g, e), ask_freq=False, name="GaN bias")
        gan_key = c.sheet_name
        c.checked = [gan_key]
        win.refresh(rerender=True)
        rows = c.condition_panel_rows()
        assert [l for l, _ in rows[0]] == ["VG_DRV", "VG_CA", "VG_PK"] and rows[0][0][1] == "-1.10 V" \
            and rows[0][1][1] == "-1.20 V", rows
        assert [l for l, _ in rows[1]] == ["IQ_DRV", "IQ_CA", ""], rows
        win.canvas.draw()
        pos = {t.get_text().split(" = ")[0]: t.get_position() for t in win.figure.texts if " = " in t.get_text()}
        assert abs(pos["VG_DRV"][0] - pos["IQ_DRV"][0]) < 1e-9 and abs(pos["VG_CA"][0] - pos["IQ_CA"][0]) < 1e-9, pos
        assert abs(pos["VG_DRV"][1] - pos["VG_PK"][1]) < 1e-9 and pos["IQ_DRV"][1] < pos["VG_DRV"][1], pos
        sizes = {t.get_fontsize() for t in win.figure.texts if " = " in t.get_text()}
        assert max(sizes) <= 8.5, sizes
        c.remove_sheet(gan_key)

    def small_window_and_fonts():
        v = win.views[0]
        title_big = [t for t in v.figure.texts if t.get_text().endswith("Pout")][0].get_fontsize()
        if not _fake():
            win.resize(760, 520)
            app.processEvents()
            assert win.width() <= 800 and win.height() <= 560, (win.width(), win.height())
            win.apply_ui_scale(760, 520)
            assert win.centralWidget().font().pointSizeF() < win._base_pt, win.centralWidget().font().pointSizeF()
        v.figure.set_size_inches(3.6, 2.6)
        v.render()
        title_small = [t for t in v.figure.texts if t.get_text().endswith("Pout")][0].get_fontsize()
        assert title_small < title_big * 0.75, (title_big, title_small)
        tick = v.renderer.ax.xaxis.get_ticklabels()[0].get_fontsize()
        assert tick < 9.5, tick
        if not _fake():
            win.resize(1400, 900)
            app.processEvents()
            win.apply_ui_scale(1500, 920)
        v.figure.set_size_inches(8, 5.5)
        v.render()

    def collapsible_panels():
        assert set(win.sections) == {"datasets", "plot", "axis", "marker", "options", "messages"}
        for key, sec in win.sections.items():
            was = sec.expanded
            sec.button.setChecked(not was)
            assert sec.expanded == (not was), key
            if not _fake():
                app.processEvents()
                assert sec.body.isVisible() == sec.expanded, key
            sec.button.setChecked(was)
        for key in ("plot", "axis", "marker"):           # fold the rest -> Datasets gets the height
            win.sections[key].set_expanded(False)
        if not _fake():
            app.processEvents()
            assert win.data_list.height() > 200, win.data_list.height()
        for key in ("plot", "axis", "marker"):
            win.sections[key].set_expanded(True)

    def fold_dataset_groups():
        def header(kind):
            return next(win.data_list.item(i) for i in range(win.data_list.count())
                        if win.data_list.item(i).data(257) == "header:" + kind)

        def items(kind):
            return [win.data_list.item(i) for i in range(win.data_list.count())
                    if win.data_list.item(i).data(257) == kind]
        win.set_list_folded("sim", False)
        win.set_list_folded("meas", False)
        win.data_list.itemClicked.emit(header("sim")) if _fake() else win.on_data_clicked(header("sim"))
        assert all(it.isHidden() for it in items("sim")) and not any(it.isHidden() for it in items("meas"))
        assert header("sim").text().startswith("▶")
        win.on_data_clicked(header("meas"))
        assert all(it.isHidden() for it in items("meas"))
        win.on_data_clicked(header("sim"))
        win.on_data_clicked(header("meas"))
        assert not any(it.isHidden() for it in items("sim") + items("meas"))
        assert header("meas").text().startswith("▼")
        win.on_data_clicked(header("sim"))                      # leave Simulation folded …
        win.refresh(rerender=True)                              # … it stays folded after a refresh
        assert all(it.isHidden() for it in items("sim"))
        win.set_list_folded("sim", False)

    def point_marker_callout():
        win.activate_view(win.views[0])
        win.data_list.setCurrentItem(item("3.6GHz"))
        only_current()
        win.graph_buttons["pae_vs_pout"].setChecked(True)
        win.on_reset()
        line = [l for l in ax().get_lines() if l.get_label() == "Measurement"][0]
        x0, y0 = line.get_xdata()[15], line.get_ydata()[15]
        ev("motion_notify_event", x0 + 0.05, y0 + 0.3)           # near (not exactly on) the point
        assert win.hover.annot.get_visible()                     # hover tooltip still works
        key("m")
        pins = c.cur_pins
        assert len(pins) == 1 and pins[0]["label"] == "Measurement" and pins[0]["x"] == x0 and pins[0]["y"] == y0, pins
        ann = win.view.overlay.pins[0]
        assert "PAE = " in ann.get_text() and "Pout = " in ann.get_text(), ann.get_text()
        key("ㅡ")                                                 # same point again (Korean IME 'm') -> no duplicate
        assert len(c.cur_pins) == 1
        # drag the callout somewhere else (the point stays)
        win.canvas.draw()
        bb = ann.get_bbox_patch().get_window_extent(win.canvas.get_renderer())
        cx, cy = (bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2
        dx0, dy0 = pins[0]["dx"], pins[0]["dy"]
        for nm, x, y in (("button_press_event", cx, cy), ("motion_notify_event", cx + 60, cy - 40),
                         ("button_release_event", cx + 60, cy - 40)):
            win.canvas.callbacks.process(nm, MouseEvent(nm, win.canvas, x, y, button=1))
        k = 72.0 / win.figure.dpi
        assert abs(pins[0]["dx"] - (dx0 + 60 * k)) < 1e-6 and abs(pins[0]["dy"] - (dy0 - 40 * k)) < 1e-6, pins[0]
        assert (pins[0]["x"], pins[0]["y"]) == (x0, y0)
        # right-click menu: font size
        fs = win.view.overlay.pins[0].get_fontsize()
        menu, acts = win.build_pin_menu(0)
        acts["bigger"].trigger()
        assert win.view.overlay.pins[0].get_fontsize() > fs
        acts["smaller"].trigger(); acts["smaller"].trigger()
        assert win.view.overlay.pins[0].get_fontsize() < fs
        acts["reset"].trigger()
        assert abs(win.view.overlay.pins[0].get_fontsize() - fs) < 1e-6
        # pins belong to the graph: switch to Gain -> none, back -> still there
        win.graph_buttons["gain_vs_pout"].setChecked(True)
        assert win.view.overlay.pins == []
        win.graph_buttons["pae_vs_pout"].setChecked(True)
        assert len(win.view.overlay.pins) == 1
        acts["delete"].trigger()
        assert c.cur_pins == []

    def backoff_label_with_value():
        win.bo_edit.setText("8")
        win.on_backoff()
        texts = [t.get_text() for t in ax().texts if t.get_text().startswith("-8dB")]
        assert texts and all(", " in t and t.endswith("%") for t in texts), texts
        win.graph_buttons["gain_vs_pout"].setChecked(True)
        texts = [t.get_text() for t in ax().texts if t.get_text().startswith("-8dB")]
        assert texts and all(t.endswith("dB") and ", " in t for t in texts), texts
        win.graph_buttons["pae_vs_pout"].setChecked(True)
        win.bo_edit.setText(""); win.on_backoff()

    def notepad_sim_files():
        from utils.paths import bundle_root
        files = [os.path.join(bundle_root(), "data", "example", "sim_data", f"{n}.txt") for n in ("3B", "3X", "3P", "3K")]
        win.set_sim_files(files)
        names = [win.sim_combo.itemText(i) for i in range(win.sim_combo.count())]
        assert names == ["모든 Set", "3B", "3X", "3P", "3K"], names
        win.data_list.setCurrentItem(item("3.6GHz"))
        only_current()
        sims = [n for n in curves() if n.startswith("Sim")]
        assert sims == ["Sim 3B · 3.60 GHz", "Sim 3X · 3.60 GHz", "Sim 3P · 3.60 GHz", "Sim 3K · 3.60 GHz"], sims
        sim_lines = [l for l in ax().get_lines() if l.get_label().startswith("Sim")]
        assert len({l.get_color() for l in sim_lines}) == 1                       # same FREQ -> same colour
        assert len({l.get_linestyle() for l in sim_lines}) == 4                   # sets told apart by line style
        meas_line = [l for l in ax().get_lines() if l.get_label() in ("Measurement",) or l.get_label().startswith("3.6GHz")][0]
        assert meas_line.get_color() == sim_lines[0].get_color(), (meas_line.get_color(), sim_lines[0].get_color())
        win.sim_combo.setCurrentIndex(win.sim_combo.findData("3P"))
        assert [n for n in curves() if n.startswith("Sim")] == ["Simulation (3P)"], curves()
        kinds = [win.data_list.item(i).data(257) for i in range(win.data_list.count())]
        assert kinds.count("sim") == 5 and "group" not in kinds, kinds        # only the chosen set 3P
        assert all(win.data_list.item(i).text().startswith("3P") for i in range(win.data_list.count())
                   if win.data_list.item(i).data(257) == "sim")
        win.sim_combo.setCurrentIndex(win.sim_combo.findData("*"))
        kinds = [win.data_list.item(i).data(257) for i in range(win.data_list.count())]
        assert kinds.count("sim") == 20 and kinds.count("group") == 4, kinds
        # handlers must survive a deleted item (Qt passes None after a list rebuild)
        win.on_data_clicked(None); win.on_data_checked(None); win.on_data_current(None)
        # back to the bundled example folder for the remaining steps
        win.c.load_simulation(os.path.join(bundle_root(), "data", "example", "simulation"))
        win.settings.setValue("sim_files", "")
        win._refresh_sim_combo()
        win.refresh(rerender=True)

    def multi_backoff_and_panel():
        win.set_layout(2)
        win.bo_edit.setText("6, 8")
        win.on_backoff()
        win.bo_edit.setText("10")
        win.on_backoff_add()
        assert c.backoffs == [6.0, 8.0, 10.0] and win.bo_edit.text() == "6, 8, 10", (c.backoffs, win.bo_edit.text())
        for v in win.visible_views:                       # every plot shows all three
            labels = {t.get_text().split(",")[0] for t in v.renderer.ax.texts if t.get_text().startswith("-")}
            assert {"-6dB", "-8dB", "-10dB"} <= labels, labels
        h_plot = win.v_split.sizes()[0]
        win.bo_toggle.setChecked(False)                   # fold the table -> plots get taller
        assert not win.bo_table.isVisible() or _fake()
        assert win.v_split.sizes()[0] > h_plot, (h_plot, win.v_split.sizes())
        win.bo_toggle.setChecked(True)
        win.on_backoff_clear()
        assert c.backoffs == [] and win.bo_table.rowCount() == 0

    def copy_plot():
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().clear()
        win.on_copy_plot()
        img = QGuiApplication.clipboard().image()
        assert not img.isNull() and img.width() > 200

    def save_png():
        from plotting.export import save_png as _save
        p = _save(win.figure, os.path.join(tempfile.mkdtemp(), "x.png"))
        assert os.path.getsize(p) > 20000

    for name, fn in [("Example Excel Open", open_example), ("Dataset 목록 선택", walk_datasets),
                     ("FREQ/VGS,CA/VGS,PK/VGS,DRV 표시", conditions), ("Measurement/Simulation Overlay", overlay_single),
                     ("PAE / Gain Graph 전환", gain_graph), ("X/Y Axis Range 변경", axis_range),
                     ("왼쪽 Drag 이동", left_drag_pan), ("휠 Zoom", wheel_zoom), ("F: 전체 보기 / Reset", f_key_fit_all),
                     ("Hover Tooltip", hover), ("V/H Marker (0.1 dBm, 0.5 %, 0.1 dB)", markers_keys_snap),
                     ("Marker Drag + 방향키", drag_and_arrows), ("값 상자 우클릭 메뉴", value_box_menu),
                     ("체크박스로 여러 Dataset 겹쳐 보기", multi_overlay), ("Psat Back-off 표", backoff),
                     ("Raw data 붙여넣기 (DUT_Pout / Gain / PAE 인식)", raw_paste),
                     ("프로젝트 저장 / 열기", project_roundtrip),
                     ("Plot 창 1~4개 (크기 균일, Plot별 PAE/Gain)", plot_panes),
                     ("Dataset 우클릭 퀵메뉴 (색·선·굵기·점)", dataset_style_menu),
                     ("Dataset 여러 개 선택 → 한꺼번에 스타일", multi_select_style),
                     ("Copy/Save = 화면의 Plot 전체 + 조건 선택", copy_all_and_conditions),
                     ("Simulation 체크 목록 / 표시 / 자동 매칭", sim_checklist),
                     ("측정 + Simulation 동시 선택 스타일", mixed_selection_style),
                     ("Measurement 여러 개 선택 (Ctrl+클릭)", multi_select_measurements),
                     ("Datasets 끌어서 순서 변경 / Delete로 삭제", reorder_and_delete),
                     ("주파수별 색상 (측정 = Sim)", colors_by_frequency),
                     ("선택한 데이터 강조 표시", highlight_selected),
                     ("Datasets 더블클릭 / F2 이름 변경", rename_in_list),
                     ("방향키 꾹 누르기 → Plot은 멈춘 뒤 한 번만 그림", arrow_keys_held),
                     ("우클릭 → 숨기기 (체크 해제)", hide_selected),
                     ("우클릭 → 이것들도 같이 보기 (추가)", show_also),
                     ("우클릭 → 이름 한꺼번에 변경", batch_rename),
                     ("Excel로 내보내기 (데이터 + 차트)", excel_export),
                     ("Marker 값 상자 끌어서 이동", drag_value_box),
                     ("여러 개 선택 → 여러 곡선 강조", multi_highlight),
                     ("Plot 곡선 클릭 → 목록이 그 항목까지 자동 스크롤", pick_scrolls_list),
                     ("각각 다른 색 / 주파수별 기본색 버튼", color_buttons),
                     ("더블클릭 → 데이터 보기/수정 (목록·곡선, Sim은 보기만)", data_editor),
                     ("데이터 표: = 계산 / 채우기 핸들 / 더블클릭 채우기", editor_formulas),
                     ("눈 아이콘: 항상 숨기기 (검색 Enter·모두 체크에도 안 나옴)", eye_always_hidden),
                     ("눈 빠르게 여러 번 클릭 → 여러 줄 선택되지 않음", eye_fast_clicks_select_nothing),
                     ("━ 선 기본 버튼 (색은 유지)", lines_default_button),
                     ("모두 해제 = 아무것도 안 그림 → 클릭 미리보기 → Space 고정", uncheck_all_then_browse_and_space),
                     ("Simulation만 골라 [선택 Plot] / 우클릭 이것들만 보기", selected_plot_simulations),
                     ("그려진 곡선 전부 선택해도 강조", multi_highlight_all_shown),
                     ("★ 안에서 Shift 다중선택 = ★ 줄만 / 체크박스 넓은 클릭 영역", fav_shift_select_and_check_zone),
                     ("Plot 3·4 = 끌어다 놓은 것만 / Plot에서 Delete = 그 Plot에서 빼기", own_plots_3_4_and_delete),
                     ("Plot에서 Ctrl/Shift+클릭 = 여러 곡선 강조", plot_ctrl_click_multi),
                     ("제목 / X·Y축 이름 바꾸기 (더블클릭, 우클릭)", plot_and_axis_titles),
                     ("붙여넣기: 인식 안 되면 맵핑 즐겨찾기 자동 / 창에서 선택", paste_uses_mapping_favourite),
                     ("선1 / 선2 즐겨찾기 버튼 (+ 우클릭으로 모양 변경)", line_favourite_buttons),
                     ("우클릭 → 데이터 정보 (추가한 때)", dataset_info_menu),
                     ("★ 즐겨찾기 구역 (끌어다 놓기 / 검색에도 표시 / 빼기)", favourites_section),
                     ("Plot 우클릭 메뉴 (빈 곳/곡선) · 우클릭 드래그 = 확대", plot_right_click_menu),
                     ("Column Mapping 즐겨찾기 (맵핑1 저장 → 우클릭 적용)", mapping_favourites),
                     ("[선택 Plot] 버튼 = 선택한 것만 표시", selected_plot_button),
                     ("↕ 정렬 (같은 이름 = 주파수 순) + Ctrl+S 덮어쓰기 저장", sort_and_quick_save),
                     ("검색 + 여러 개 선택 중 곡선 클릭 → 그 곡선 강조/선택", pick_with_search_and_multiselect),
                     ("Marker 더블클릭 → 값 직접 입력", marker_type_value),
                     ("여러 개 선택 + F2 = 한꺼번에 이름 / Space = 한꺼번에 표시", f2_space_on_several),
                     ("붙여넣기 이름: 바로 전 이름 기억", paste_remembers_name),
                     ("Ctrl+Z 되돌리기 / Ctrl+Y 다시 실행", undo_redo),
                     ("Axis Reset 버튼 (모든 Plot)", axis_reset_button),
                     ("검색 중엔 접힌 구역도 펼쳐서 표시", search_keeps_sections_open),
                     ("Plot 곡선 클릭 = 강조 + 목록 선택 / 빈 곳 = 해제", plot_click_pick_and_clear),
                     ("Datasets 선택해도 스크롤 위치 유지", list_keeps_scroll),
                     ("Datasets 검색 필터 + 결과만 Plot", search_filter),
                     ("검색 → Ctrl+A → 우클릭 색 (각각 / 같은 색 / 기본) + 간단 Legend", filter_select_color),
                     ("조건 표시 정렬 (IQ ↓ VG) + GaN VG 음수", header_alignment_and_gan_bias),
                     ("창 작게 + 글꼴 비율 축소", small_window_and_fonts),
                     ("왼쪽 메뉴 접기/펼치기", collapsible_panels),
                     ("M: 포인트 말풍선 (이동 / 글자 크기)", point_marker_callout),
                     ("Back-off 표시에 y값", backoff_label_with_value),
                     ("메모장 Sim 파일 (3B/3X/3P/3K) 지정", notepad_sim_files),
                     ("Back-off 여러 개 + 패널 크기/접기", multi_backoff_and_panel),
                     ("Datasets: Measurement / Simulation 접기", fold_dataset_groups), ("Copy Plot", copy_plot), ("PNG Export", save_png)]:
        step(name, fn)

    global LAST_REPORT
    n_fail = sum(1 for _, ok, _ in results if not ok)
    from version import VERSION_LABEL
    lines = [f"==== PA Measurement Analyzer {VERSION_LABEL} - GUI smoke test ===="]
    lines += [f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  -> {e}" if e else "") for n, ok, e in results]
    lines.append(f"==== {len(results) - n_fail}/{len(results)} passed ====")
    LAST_REPORT = "\n".join(lines) + "\n"
    print("\n" + LAST_REPORT)
    return 1 if n_fail else 0
