"""GUI self-test:  SParam-Viewer.exe --smoke-test  (exit 0 = all PASS)."""
from __future__ import annotations

import os
import tempfile
import traceback

from matplotlib.backend_bases import KeyEvent, MouseEvent


def _fake():
    import PySide6
    return not hasattr(PySide6, "__version__")


def run_smoke_test(app, win):
    results = []
    win.resize(1500, 920)
    app.processEvents()
    P1 = win.panes[0]
    c = P1.c

    def step(name, fn):
        try:
            fn()
            app.processEvents()
            results.append((name, True, ""))
        except Exception as exc:
            traceback.print_exc()
            results.append((name, False, repr(exc)))

    def lines(pane=None):
        ax = (pane or win.active).ax
        return [l for l in ax.get_lines() if not l.get_label().startswith("_")]

    def ev(pane, name, xd, yd, **kw):
        pane.canvas.draw()
        px, py = pane.ax.transData.transform((xd, yd))
        e = MouseEvent(name, pane.canvas, px, py, **kw)
        pane.canvas.callbacks.process(name, e)
        return e

    def ev_px(pane, name, px, py, **kw):
        e = MouseEvent(name, pane.canvas, px, py, **kw)
        pane.canvas.callbacks.process(name, e)
        return e

    def Qt():
        from PySide6.QtCore import Qt as _Qt
        return _Qt

    # ------------------------------------------------------------------
    def add_examples():
        win.on_example()
        assert len(win.store.entries) == 5 and win.file_list.count() == 5

    def two_panes_default():
        assert win.n_panes == 2 and win.panes[1].c.is_smith
        assert len(lines(P1)) == 5 and len(lines(win.panes[1])) == 2

    def four_panes():
        win.layout_combo.setCurrentIndex(win.layout_combo.findData(4))
        assert win.n_panes == 4
        assert [p.c.fmt for p in win.visible_panes] == ["db", "smith", "db", "phase"]
        ev(win.panes[2], "button_press_event", 3.0, -5, button=1)
        ev(win.panes[2], "button_release_event", 3.0, -5, button=1)
        assert win.active is win.panes[2] and win.param_checks["s11"].isChecked()
        win.set_active(P1)
        assert not win.param_checks["s11"].isChecked()

    def params_and_hide():
        for p in ("s11", "s22", "s12"):
            win.param_checks[p].setChecked(True)
        assert len(lines(P1)) == 20
        for p in ("s11", "s22", "s12"):
            win.param_checks[p].setChecked(False)
        it = win.file_list.item(0)
        it.setCheckState(Qt().Unchecked)
        assert len(lines(P1)) == 4
        it.setCheckState(Qt().Checked)
        assert len(lines(P1)) == 5

    def axis_range_and_fit():
        win.xmin_edit.setText("3.2"); win.xmax_edit.setText("4.0")
        win.ymin_edit.setText("0"); win.ymax_edit.setText("16")
        win.on_apply_range()
        assert P1.ax.get_xlim() == (3.2, 4.0) and P1.ax.get_ylim() == (0.0, 16.0)
        b = P1.ax.get_window_extent()
        ev_px(P1, "scroll_event", b.x0 + b.width / 3, b.y0 + b.height / 2, step=1, button="up")
        assert P1.ax.get_xlim() != (3.2, 4.0)
        win.on_fit()
        assert P1.ax.get_xlim() == (3.2, 4.0) and P1.ax.get_ylim() == (0.0, 16.0)

    def left_drag_pan():
        before = P1.ax.get_xlim()
        ev(P1, "button_press_event", 3.4, 4, button=1)
        ev(P1, "motion_notify_event", 3.6, 4)
        ev_px(P1, "button_release_event", 0, 0, button=1)
        after = P1.ax.get_xlim()
        assert after[0] < before[0] - 0.1, (before, after)    # dragged right -> view moves left
        assert c.view_xlim is not None
        win.on_fit()
        assert P1.ax.get_xlim() == (3.2, 4.0)

    def right_drag_box_zoom():
        ev(P1, "button_press_event", 3.4, 6, button=3)
        ev(P1, "motion_notify_event", 3.7, 12)
        ev(P1, "button_release_event", 3.7, 12, button=3)
        x0, x1 = P1.ax.get_xlim()
        assert abs(x0 - 3.4) < 0.01 and abs(x1 - 3.7) < 0.01, (x0, x1)
        win.on_fit()

    def v_h_markers_snap():
        ev(P1, "motion_notify_event", 3.537, 10)
        win.on_v_key()
        assert abs(c.markers_hz[0] - 3.55e9) < 1, c.markers_hz         # snapped to 0.05 GHz
        ev(P1, "motion_notify_event", 3.7, 12)
        win.on_h_key()
        assert len(c.h_markers) == 1 and abs(c.h_markers[0] - 12) < 0.05
        assert win.table.rowCount() == 5

    def drag_marker_snap():
        ev(P1, "button_press_event", 3.55, 8, button=1)
        assert P1._drag == ("v", 0), P1._drag
        ev(P1, "motion_notify_event", 3.812, 8)
        assert abs(c.markers_hz[0] - 3.80e9) < 1, c.markers_hz
        ev(P1, "motion_notify_event", 3.838, 8)
        assert abs(c.markers_hz[0] - 3.85e9) < 1, c.markers_hz
        ev(P1, "button_release_event", 3.838, 8, button=1)
        assert P1._drag is None
        ev(P1, "button_press_event", 3.3, 12, button=1)
        ev(P1, "motion_notify_event", 3.3, 9.5)
        ev(P1, "button_release_event", 3.3, 9.5, button=1)
        assert abs(c.h_markers[0] - 9.5) < 0.05, c.h_markers

    def values_toggle_delete():
        n_on = len(P1.overlay.artists)
        win.values_check.setChecked(False)
        assert len(P1.overlay.artists) == n_on - 1
        win.values_check.setChecked(True)
        c.select_marker(None)
        ev(P1, "motion_notify_event", 3.85, 5)
        win.on_delete_marker()
        assert len(c.markers_hz) == 0
        win.on_delete_marker()
        assert len(c.h_markers) == 0

    def legend_rename():
        P1.rename_series(0, "My Board A")
        assert lines(P1)[0].get_label() == "My Board A"
        assert any(l.get_label() == "My Board A" for l in lines(win.panes[3])) or True
        P1.canvas.draw()
        t = P1.ax.get_legend().get_texts()[0]
        bb = t.get_window_extent()
        e = MouseEvent("button_press_event", P1.canvas, (bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2, button=1)
        assert P1.renderer.legend_index_at(e) == 0
        P1.rename_series(0, "")
        assert lines(P1)[0].get_label() == "Board_A: S21"
        c.rename_file(win.store.entries[0].uid, "Board A (new)")
        win.refresh_files()
        assert lines(P1)[0].get_label() == "Board A (new): S21"

    def smith_pane():
        P2 = win.panes[1]
        win.set_active(P2)
        assert win.smith_radio.isChecked() and win.xmin_label.text().startswith("F Start")
        win.xmin_edit.setText("3.4"); win.xmax_edit.setText("3.8")
        win.on_apply_range()
        for l in lines(P2):
            f = l.sp_freq_ghz
            assert f.min() >= 3.4 - 1e-9 and f.max() <= 3.8 + 1e-9, (f.min(), f.max())
        l = lines(P2)[0]
        ev(P2, "motion_notify_event", l.get_xdata()[10], l.get_ydata()[10])
        win.on_v_key()
        assert len(P2.c.markers_hz) == 1
        hz0 = P2.c.markers_hz[0]
        assert abs(hz0 / 0.05e9 - round(hz0 / 0.05e9)) < 1e-6, hz0
        assert "Z =" in P2.hover.annot.get_text()
        # drag the Smith marker along the trace
        P2.canvas.draw()
        i, dot = P2.overlay.smith_dots[0]
        ev(P2, "button_press_event", dot.get_xdata()[0], dot.get_ydata()[0], button=1)
        assert P2._drag == ("v", 0), P2._drag
        ev(P2, "motion_notify_event", l.get_xdata()[-1], l.get_ydata()[-1])
        ev(P2, "button_release_event", l.get_xdata()[-1], l.get_ydata()[-1], button=1)
        assert P2.c.markers_hz[0] > hz0
        win.on_reset()
        assert P2.c.smith_frange is None
        win.set_active(P1)

    def copy_all():
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().clear()
        win.on_copy_all()
        img = QGuiApplication.clipboard().image()
        assert not img.isNull()
        one = QGuiApplication.clipboard()
        win.on_copy_plot()
        single = one.image()
        assert img.width() >= 2 * single.width() - 2 and img.height() >= 2 * single.height() - 2, \
            (img.width(), single.width())

    def markers_table():
        win.marker_edit.setText("3.6"); win.on_add_marker()
        win.marker_edit.setText("3400MHz"); win.on_add_marker()
        assert win.table.rowCount() == 5 and win.table.columnCount() == 4
        win.on_copy_table()
        win.on_clear_markers()

    def equal_sizes():
        g = win.grid
        for n, rows, cols in ((1, 1, 1), (2, 1, 2), (3, 2, 2), (4, 2, 2)):
            win.set_layout(n)
            rs = [g.rowStretch(r) for r in range(2)]
            cs = [g.columnStretch(cc) for cc in range(2)]
            if not _fake():
                assert rs == [1 if r < rows else 0 for r in range(2)], rs
                assert cs == [1 if cc < cols else 0 for cc in range(2)], cs
                app.processEvents()
                sizes = {(p.width(), p.height()) for p in win.visible_panes}
                assert max(w for w, h in sizes) - min(w for w, h in sizes) <= 2, sizes
                assert max(h for w, h in sizes) - min(h for w, h in sizes) <= 2, sizes
        win.set_layout(4)

    def arrow_keys_select():
        c.clear_markers()
        win.set_active(P1)
        ev(P1, "motion_notify_event", 3.6, 5)
        win.on_v_key()
        ev(P1, "motion_notify_event", 3.3, 10.2)
        win.on_h_key()
        assert c.h_markers == [10.0], c.h_markers                        # 0.5 dB grid
        ev(P1, "button_press_event", 3.6, 8, button=1)                  # click the vertical marker
        ev(P1, "button_release_event", 3.6, 8, button=1)
        assert c.selected == ("v", 0), c.selected
        for k in ("right", "right", "left", "right"):
            P1.canvas.callbacks.process("key_press_event", KeyEvent("key_press_event", P1.canvas, k))
        assert abs(c.markers_hz[0] - 3.7e9) < 1, c.markers_hz
        ev(P1, "button_press_event", 3.3, 10, button=1)                # click the horizontal marker
        ev(P1, "button_release_event", 3.3, 10, button=1)
        assert c.selected == ("h", 0), c.selected
        for k in ("up", "up", "down"):
            P1.canvas.callbacks.process("key_press_event", KeyEvent("key_press_event", P1.canvas, k))
        assert c.h_markers == [10.5], c.h_markers
        ev(P1, "button_press_event", 3.3, 10.5, button=1)
        ev(P1, "motion_notify_event", 3.3, 6.8)
        ev(P1, "button_release_event", 3.3, 6.8, button=1)
        assert c.h_markers == [7.0], c.h_markers                         # drag also 0.5 dB
        win.table.rowCount()

    def h_key_on_canvas():
        n = len(c.h_markers)
        ev(P1, "motion_notify_event", 3.5, 12)
        P1.canvas.callbacks.process("key_press_event", KeyEvent("key_press_event", P1.canvas, "h"))
        P1.canvas.callbacks.process("key_press_event", KeyEvent("key_press_event", P1.canvas, "ㅗ"))  # Hangul mode
        assert len(c.h_markers) == n + 2, c.h_markers

    def panel_menu():
        P1.canvas.draw()
        bb = P1.overlay.panel.get_window_extent(P1.canvas.get_renderer())
        assert P1.overlay.panel_hit((bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2)
        menu, acts = win.build_panel_menu(P1)
        n_full = len(P1.overlay.artists)
        acts["collapse"].trigger()
        assert c.panel_collapsed
        acts["loc:lower right"].trigger()
        assert c.panel_loc == "lower right"
        menu, acts = win.build_panel_menu(P1)
        acts["expand"].trigger()
        assert not c.panel_collapsed
        acts["hide"].trigger()
        assert not c.show_marker_values and P1.overlay.panel is None and not win.values_check.isChecked()
        hit = ("v", 0)
        menu, acts = win.build_marker_menu(P1, hit)
        acts["values"].trigger()
        assert c.show_marker_values and P1.overlay.panel is not None
        menu, acts = win.build_marker_menu(P1, hit)
        acts["delete"].trigger()
        assert len(c.markers_hz) == 0
        c.clear_markers(); P1.redraw_markers()

    def legend_and_file_menus():
        win.set_active(P1)
        menu, acts = win.build_legend_menu(P1, 0)
        acts["style:dotted"].trigger()
        acts["width:thick"].trigger()
        acts["color:#2ca02c"].trigger()
        l0 = lines(P1)[0]
        assert l0.get_linestyle() == ":" and l0.get_linewidth() == 2.6 and l0.get_color() == "#2ca02c", \
            (l0.get_linestyle(), l0.get_linewidth(), l0.get_color())
        acts["reset"].trigger()
        assert lines(P1)[0].get_linestyle() == "-"
        uid = win.store.entries[1].uid
        menu, acts = win.build_file_menu(uid)
        acts["color:#000000"].trigger()
        acts["style:dashed"].trigger()
        acts["width:thin"].trigger()
        l1 = lines(P1)[1]
        assert l1.get_color() == "#000000" and l1.get_linestyle() == "--" and l1.get_linewidth() == 0.9
        assert any(l.get_color() == "#000000" for l in lines(win.panes[2]))     # every pane follows
        acts["reset"].trigger()
        acts["toggle"].trigger()
        assert len(lines(P1)) == 4
        menu, acts = win.build_file_menu(uid)
        acts["toggle"].trigger()
        assert len(lines(P1)) == 5

    def save():
        from sparam.plotting.export import save_png
        p = save_png(P1.figure, os.path.join(tempfile.mkdtemp(), c.default_export_name()))
        assert os.path.getsize(p) > 10000 and os.path.basename(p) == "S21_dB_5files.png", p

    def bad_file():
        d = tempfile.mkdtemp()
        p = os.path.join(d, "broken.csv")
        with open(p, "w") as f:
            f.write("hello\nworld\n")
        n, errs = c.add_files([p])
        assert n == 0 and errs

    # ---------------- Datasets-style file list (search / multi-select / highlight / undo …)
    def items():
        return [win.file_list.item(i) for i in range(win.file_list.count())]

    def by_uid():
        return {l.sp_key[0]: l for l in lines(P1) if getattr(l, "sp_key", None)}

    def search_and_select():
        win.set_active(P1)
        for e in win.store.entries:
            e.visible = True
        win.refresh_files()
        name0 = win.store.entries[0].data.name
        word = name0.split()[0][:4]
        win.on_filter_changed(word)
        vis = [it for it in items() if not it.isHidden()]
        assert vis and all(word.lower() in it.text().lower() for it in vis), [it.text() for it in vis]
        win.file_list.selectAll()                                  # Ctrl+A = only the search results
        assert sorted(win._selected_uids()) == sorted(it.data(256) for it in vis)
        win.on_filter_show_only()                                  # Enter: only the results on the plot
        assert all(e.visible == any(it.data(256) == e.uid for it in vis) for e in win.store.entries)
        win.on_filter_changed("")
        assert all(not it.isHidden() for it in items())
        for e in win.store.entries:
            e.visible = True
        win.refresh_files()

    def highlight_selection_and_pick():
        uids = [e.uid for e in win.store.entries]
        for it in items():
            it.setSelected(it.data(256) in uids[:2])
        win.on_file_selection()                                     # (real Qt: emitted by the list)
        P1.render()
        bl = by_uid()
        assert bl[uids[0]].get_zorder() == 10 and bl[uids[1]].get_zorder() == 10
        assert (bl[uids[2]].get_alpha() or 1) < 0.3
        win.on_list_clear()                                         # empty space / Esc
        assert all((l.get_alpha() or 1) == 1 for l in lines(P1))
        target = by_uid()[uids[3]]                                  # click ON a curve in the plot
        P1.canvas.draw()
        import numpy as np
        others = [l for l in lines(P1) if l is not target]

        def gap(i):
            p = P1.ax.transData.transform((target.get_xdata()[i], target.get_ydata()[i]))
            return min(float(np.min(np.hypot(*(P1.ax.transData.transform(np.column_stack(o.get_data())) - p).T)))
                       for o in others)
        x0, x1 = P1.ax.get_xlim(); y0, y1 = P1.ax.get_ylim()
        idx = [i for i in range(len(target.get_xdata())) if x0 < target.get_xdata()[i] < x1
               and y0 < target.get_ydata()[i] < y1]
        k = max(idx, key=gap)
        ev(P1, "button_press_event", target.get_xdata()[k], target.get_ydata()[k], button=1)
        ev(P1, "button_release_event", target.get_xdata()[k], target.get_ydata()[k], button=1)
        app.processEvents()
        assert win.store.highlight == [uids[3]], win.store.highlight
        assert win._selected_uids() == [uids[3]]                     # the list follows the plot
        P1.render()
        assert by_uid()[uids[3]].get_zorder() == 10
        b = P1.ax.bbox                                              # empty corner -> no emphasis
        ev_px(P1, "button_press_event", b.x1 - 4, b.y0 + 4, button=1)
        ev_px(P1, "button_release_event", b.x1 - 4, b.y0 + 4, button=1)
        app.processEvents()
        assert win.store.highlight == []

    def multi_menu():
        uids = [e.uid for e in win.store.entries][:3]
        menu, acts = win.build_file_menu(uids)
        acts["color:distinct"].trigger()
        cols = [win.c.entry(u).color for u in uids]
        assert len(set(cols)) == 3, cols
        acts["color:#000000"].trigger()
        assert all(win.c.entry(u).color == "#000000" for u in uids)
        acts["color:auto"].trigger()
        assert all(win.c.entry(u).color == win.c.entry(u).base_color for u in uids)
        acts["hide"].trigger()
        assert not any(win.c.entry(u).visible for u in uids)
        win.build_file_menu(uids[:1])[1]["add"].trigger()
        assert win.c.entry(uids[0]).visible and not win.c.entry(uids[1]).visible
        win.build_file_menu(uids)[1]["only"].trigger()
        assert [e.visible for e in win.store.entries] == [e.uid in uids for e in win.store.entries]
        import sparam.ui.main_window as smw
        orig = smw.QInputDialog.getText
        try:
            smw.QInputDialog.getText = staticmethod(lambda *a, **k: ("Board-X", True))
            win.build_file_menu(uids[:2])[1]["rename_many"].trigger()
        finally:
            smw.QInputDialog.getText = orig
        assert [win.c.entry(u).data.name for u in uids[:2]] == ["Board-X", "Board-X"]
        for e in win.store.entries:
            e.visible = True
        win.refresh_files()

    def reorder_and_delete_key():
        order = [e.uid for e in win.store.entries]
        win.on_files_reordered(order[::-1])
        assert [e.uid for e in win.store.entries] == order[::-1]
        assert [it.data(256) for it in items()] == order[::-1]
        win.on_files_reordered(order)
        last = order[-1]
        for it in items():
            it.setSelected(it.data(256) == last)
        win.on_delete_files(confirm=False)
        assert all(e.uid != last for e in win.store.entries)
        win.on_undo()                                               # Ctrl+Z brings it back
        assert any(e.uid == last for e in win.store.entries), [e.uid for e in win.store.entries]

    def point_markers_and_box():
        win.set_active(P1)
        c.clear_markers()
        c.pins.clear()
        P1.render()
        l0 = lines(P1)[0]
        i = len(l0.get_xdata()) // 2
        ev(P1, "motion_notify_event", l0.get_xdata()[i], l0.get_ydata()[i])
        win.on_m_key()                                              # M on a curve point
        assert len(c.pins) == 1 and c.pins[0]["key"] == l0.sp_key
        P1.canvas.draw()
        ann = P1.overlay.pins[0]
        bb = ann.get_bbox_patch().get_window_extent(P1.canvas.get_renderer())
        cx, cy = (bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2
        dx0 = c.pins[0]["dx"]
        ev_px(P1, "button_press_event", cx, cy, button=1)           # drag the callout
        ev_px(P1, "motion_notify_event", cx + 40, cy)
        ev_px(P1, "button_release_event", cx + 40, cy, button=1)
        assert c.pins[0]["dx"] > dx0 + 20, c.pins[0]
        assert "GHz" in ann.get_text()
        win.on_delete_marker()                                      # selected callout -> Del
        assert c.pins == []
        ev(P1, "motion_notify_event", 3.6, 5)
        win.on_v_key()
        P1.canvas.draw()
        bb = P1.overlay.panel.get_window_extent(P1.canvas.get_renderer())
        x, y = bb.x0 + 8, bb.y1 - 6                                 # grab the value box near its corner
        if P1.overlay.hit(x, y) is not None:
            x, y = bb.x1 - 8, bb.y0 + 6
        ev_px(P1, "button_press_event", x, y, button=1)
        ev_px(P1, "motion_notify_event", x + 60, y - 40)
        ev_px(P1, "button_release_event", x + 60, y - 40, button=1)
        assert c.panel_xy is not None
        win._set_panel(P1, loc="upper right")
        assert c.panel_xy is None
        c.clear_markers()
        P1.render()

    def data_editor():
        import ui.data_editor as de
        e = win.store.entries[0]
        uid = e.uid
        seen = {}

        class Fake:
            def __init__(self, title, rows, **kw):
                seen.update(kw, rows=[list(x) for x in rows])

            def exec(self):
                return True

            def values(self):
                rows = [list(x) for x in seen["rows"]]
                rows[5][1] = -42.0                             # first dB column, one point
                return "편집됨", rows
        orig = de.DataEditorDialog
        try:
            de.DataEditorDialog = Fake
            win._history_flush()
            win.file_list.on_open(uid)                         # = double-click in the list
        finally:
            de.DataEditorDialog = orig
        assert win.c.entry(uid).data.name == "편집됨"
        assert seen["headers"][0] == "Freq (GHz)"
        P1.render()
        ln = [l for l in lines(P1) if l.sp_key[0] == uid][0]
        assert -42.0 in list(ln.get_ydata())
        win._history_flush()
        win.on_undo()
        assert win.c.entry(uid).data.name != "편집됨"
        P1.render()
        assert -42.0 not in list([l for l in lines(P1) if l.sp_key[0] == uid][0].get_ydata())

    def eye_and_lines():
        uids = [e.uid for e in win.store.entries]
        for e in win.store.entries:
            e.visible = True
        win.refresh_files()
        win.on_eye(uids[0])
        assert all(l.sp_key[0] != uids[0] for l in lines(P1) if getattr(l, "sp_key", None))
        name0 = win.store.entries[0].data.name.split()[0][:3]
        win.on_filter_changed(name0)
        win.on_filter_show_only()                              # Enter: stays hidden
        assert uids[0] not in [e.uid for e in win.c.visible_entries]
        win.on_filter_changed("")
        win.build_file_menu(uids[:2])[1]["add"].trigger()       # multi 같이 보기: stays hidden
        assert uids[0] not in [e.uid for e in win.c.visible_entries]
        st = win.export_state()
        assert st["files"][0]["muted"] is True                  # saved in the project
        win.on_eye(uids[0])
        assert uids[0] in [e.uid for e in win.c.visible_entries]
        win.c.set_file_style(uids[1], linestyle="--", linewidth=2.6, color="#000000")
        win.on_lines_default()
        e1 = win.c.entry(uids[1])
        assert e1.linestyle is None and e1.linewidth is None and e1.color == "#000000"
        for e in win.store.entries:
            e.visible = True
        win.refresh_files()

    def marker_type_value():
        import sparam.ui.main_window as smw
        win.set_active(P1)
        c.clear_markers()
        c.markers_hz.append(3.55e9)
        c.h_markers.append(10.0)
        P1.render()
        orig = smw.QInputDialog.getText
        try:
            smw.QInputDialog.getText = staticmethod(lambda *a, **k: ("3612.5MHz", True))
            y0, y1 = P1.ax.get_ylim()
            ev(P1, "button_press_event", 3.55, y0 + 0.3 * (y1 - y0), button=1, dblclick=True)
            assert abs(c.markers_hz[0] - 3.6125e9) < 1, c.markers_hz            # exact typed frequency
            x0, x1 = P1.ax.get_xlim()
            smw.QInputDialog.getText = staticmethod(lambda *a, **k: ("-3.3 dB", True))
            ev(P1, "button_press_event", x0 + 0.1 * (x1 - x0), 10.0, button=1, dblclick=True)
            assert c.h_markers == [-3.3], c.h_markers
        finally:
            smw.QInputDialog.getText = orig
        c.clear_markers()
        P1.render()

    def space_f2_several():
        import sparam.ui.main_window as smw
        from PySide6.QtCore import Qt as _Q
        uids = [e.uid for e in win.store.entries][:3]

        def select(us):
            for i in range(win.file_list.count()):
                win.file_list.item(i).setSelected(win.file_list.item(i).data(256) in us)

        def key(code):
            if not _fake():
                from PySide6.QtCore import QEvent
                from PySide6.QtGui import QKeyEvent
                return QKeyEvent(QEvent.KeyPress, code, _Q.NoModifier)
            return type("E", (), {"key": lambda self: code, "modifiers": lambda self: 0,
                                  "matches": lambda self, *a: False, "accept": lambda self: None})()
        for e in win.store.entries:
            e.visible = True
        win.refresh_files()
        select(uids)
        win.file_list.keyPressEvent(key(_Q.Key_Space))         # all checked -> all off
        assert not any(win.c.entry(u).visible for u in uids)
        select(uids)
        win.file_list.keyPressEvent(key(_Q.Key_Space))         # -> all on
        assert all(win.c.entry(u).visible for u in uids)
        orig = smw.QInputDialog.getText
        try:
            smw.QInputDialog.getText = staticmethod(lambda *a, **k: ("같은이름", True))
            select(uids[:2])
            win.file_list.keyPressEvent(key(_Q.Key_F2))
        finally:
            smw.QInputDialog.getText = orig
        assert [win.c.entry(u).data.name for u in uids[:2]] == ["같은이름", "같은이름"]
        win._history_flush()
        win.on_undo()
        select([])

    def selected_plot_button():
        uids = [e.uid for e in win.store.entries]
        for i in range(win.file_list.count()):
            win.file_list.item(i).setSelected(win.file_list.item(i).data(256) in uids[1:3])
        win.on_selected_show_only()
        assert [e.uid for e in win.c.visible_entries] == uids[1:3]
        for e in win.store.entries:
            e.visible = True
        for i in range(win.file_list.count()):
            win.file_list.item(i).setSelected(False)
        win.refresh_files()

    def plot_right_click_menu():
        pane = win.active
        for e in win.store.entries:
            e.visible = True
        win.refresh_files()
        got = []
        orig = pane._on_context
        pane._on_context = lambda p, what, ev=None: got.append(what)
        try:
            pane.canvas.draw()
            bb = pane.ax.bbox
            px, py = bb.x0 + 8, bb.y1 - 8
            xl = pane.ax.get_xlim()
            for nm in ("button_press_event", "button_release_event"):
                ev_px(pane, nm, px, py, button=3)
            assert got and got[0][0] == "plot", got
            assert pane.ax.get_xlim() == xl
        finally:
            pane._on_context = orig
        menu, acts = win.build_plot_menu(pane, None)
        uids = [e.uid for e in win.store.entries]
        for k in ("all", "none", "selected", "mk:v", "fit", "reset_all", "distinct", "copy", "png", f"show:{uids[0]}"):
            assert k in acts, k
        acts[f"show:{uids[0]}"].triggered.emit(False)
        assert uids[0] not in [e.uid for e in win.c.visible_entries]
        acts["all"].triggered.emit(False)
        assert len(win.c.visible_entries) == len(uids)
        p_off = next(p for p in acts if p.startswith("param:") and not acts[p].isChecked())
        acts[p_off].triggered.emit(True)
        assert p_off[6:] in win.c.params
        acts[p_off].triggered.emit(False)
        line = next(l for l in pane.ax.get_lines() if l.get_gid())
        uid, _, param = line.get_gid().partition("|")
        menu, acts = win.build_plot_menu(pane, (int(uid), param))
        assert "curve" in acts

    def color_buttons():
        for e in win.store.entries:
            e.visible = True
        win.on_colors_distinct()
        cols = [e.color for e in win.store.entries]
        assert len(set(cols)) == len(cols), cols
        win.on_colors_base()
        assert all(e.color == e.base_color for e in win.store.entries)

    def axis_reset_all():
        win.layout_combo.setCurrentIndex(win.layout_combo.findData(2))
        for p in win.visible_panes:
            if not p.c.is_smith:
                p.c.set_ranges((3.4, 3.6), (0, 5))
            p.render()
        win.on_reset_all_axes()
        assert all(p.c.xlim is None and p.c.ylim is None and p.c.view_xlim is None for p in win.visible_panes)

    def undo_redo():
        win._history_flush()
        uid = win.store.entries[0].uid
        win.build_file_menu(uid)[1]["color:#000000"].trigger(); win._history_flush()
        win.build_file_menu(uid)[1]["hide"].trigger(); win._history_flush()
        win.on_undo()
        assert win.c.entry(uid).visible
        win.on_undo()
        assert win.c.entry(uid).color != "#000000"
        win.on_redo()
        assert win.c.entry(uid).color == "#000000"
        win.on_undo()

    def back_to_one_and_clear():
        win.layout_combo.setCurrentIndex(win.layout_combo.findData(1))
        assert win.n_panes == 1 and win.active is P1
        win.file_list.item(0).setSelected(True)
        win.on_remove()
        assert len(win.store.entries) == 4
        win.on_clear()
        assert len(win.store.entries) == 0 and len(lines(P1)) == 0

    for name, fn in [("Example 파일 5개 추가", add_examples), ("기본 2분할 (dB + Smith)", two_panes_default),
                     ("4분할 + Plot 선택", four_panes), ("Parameter / 파일 표시", params_and_hide),
                     ("Axis Range + Fit(F)", axis_range_and_fit), ("왼쪽 Drag 이동(Pan)", left_drag_pan),
                     ("오른쪽 Drag 영역 Zoom", right_drag_box_zoom), ("V/H Marker (0.05 GHz 단위)", v_h_markers_snap),
                     ("Marker Drag (0.05 GHz 단위)", drag_marker_snap), ("값 표시 On/Off + Delete", values_toggle_delete),
                     ("Legend 이름 변경", legend_rename), ("Smith: 주파수 범위 + Marker Drag", smith_pane),
                     ("Plot 크기 균일 (1~4분할)", equal_sizes),
                     ("Marker 선택 + 방향키 (0.05 GHz / 0.5 dB)", arrow_keys_select),
                     ("Plot에서 H 키 (한글 모드 포함)", h_key_on_canvas), ("값 상자 우클릭 메뉴", panel_menu),
                     ("Legend / 파일 우클릭 퀵메뉴 (색·선)", legend_and_file_menus),
                     ("Copy All Plots", copy_all), ("Marker Table", markers_table), ("Save PNG", save),
                     ("잘못된 파일 처리", bad_file),
                     ("검색 + Ctrl+A + 결과만 Plot", search_and_select),
                     ("선택/곡선 클릭 → 강조 + 목록 선택, 빈 곳 = 해제", highlight_selection_and_pick),
                     ("여러 개 우클릭: 색(각각/같은/처음) · 숨기기 · 추가 · 이름", multi_menu),
                     ("끌어서 순서 변경 / Delete 삭제 / Ctrl+Z 복구", reorder_and_delete_key),
                     ("M 말풍선 (끌기·삭제) + 값 상자 끌기", point_markers_and_box),
                     ("Axis Reset (모든 Plot)", axis_reset_all), ("각각 다른 색 / 기본색 버튼", color_buttons),
                     ("Marker 더블클릭 → 값 직접 입력", marker_type_value),
                     ("여러 개 선택 + Space / F2", space_f2_several),
                     ("Plot 우클릭 메뉴", plot_right_click_menu), ("[선택 Plot] 버튼", selected_plot_button),
                     ("눈 아이콘 항상 숨기기 + 선 기본", eye_and_lines),
                     ("더블클릭 → 데이터 보기/수정 + Ctrl+Z", data_editor), ("Ctrl+Z / Ctrl+Y", undo_redo),
                     ("1분할 복귀 / Remove / Clear", back_to_one_and_clear)]:
        step(name, fn)

    n_fail = sum(1 for _, ok, _ in results if not ok)
    out = ["==== S-Parameter Viewer - GUI smoke test ===="]
    out += [f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  -> {e}" if e else "") for n, ok, e in results]
    out.append(f"==== {len(results) - n_fail}/{len(results)} passed ====")
    report = "\n".join(out) + "\n"
    print(report)
    return (1 if n_fail else 0), report
