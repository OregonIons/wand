import asyncio
import logging
import numpy as np
import time
import functools

import pyqtgraph as pg
import pyqtgraph.dockarea as dock
from PyQt5 import QtGui

from artiq.protocols.pc_rpc import AsyncioClient as RPCClient

from wand.tools import WLMMeasurementStatus


logger = logging.getLogger(__name__)

REGULAR_UPDATE_PRIORITY = 3
FAST_MODE_PRIORITY = 2


class LaserDisplay:
    """ Diagnostics for one laser """

    def __init__(self, display_name, gui):
        self._gui = gui
        self.display_name = display_name
        self.laser = gui.config["display_names"][display_name]
        self.server = ""

        self.wake_loop = asyncio.Event()

        self.dock = dock.Dock(self.display_name, autoOrientation=False)
        self.layout = pg.GraphicsLayoutWidget(border=(80, 80, 80))

        # create widgets
        self.colour = "ffffff"  # will be set later depending on laser colour

        self.detuning = pg.LabelItem("")
        self.detuning.setText("-", color="ffffff", size="64pt")

        self.frequency = pg.LabelItem("")
        self.frequency.setText("-", color="ffffff", size="12pt")

        self.name = pg.LabelItem("")
        self.name.setText(display_name, color="ffffff", size="32pt")

        self.osa = pg.PlotItem()
        self.osa.hideAxis('bottom')
        self.osa.showGrid(y=True)
        self.osa_curve = self.osa.plot(pen='y', color=self.colour)

        self.fast_mode = QtGui.QCheckBox("Fast mode")
        self.auto_exposure = QtGui.QCheckBox("Auto expose")

        self.exposure = [QtGui.QSpinBox() for _ in range(2)]
        for idx in range(2):
            self.exposure[idx].setSuffix(" ms")
            self.exposure[idx].setRange(0, 0)

        self.lock_status = QtGui.QLineEdit()
        self.lock_status.setReadOnly(True)

        self.f_ref = QtGui.QDoubleSpinBox()
        self.f_ref.setSuffix(" THz")
        self.f_ref.setDecimals(7)
        self.f_ref.setSingleStep(1e-6)
        self.f_ref.setRange(0., 1000.)

        # context menu
        self.menu = QtGui.QMenu()
        self.ref_editable = QtGui.QAction("Enable reference changes",
                                          self.dock)
        self.ref_editable.setCheckable(True)
        self.menu.addAction(self.ref_editable)

        for label in [self.detuning, self.name, self.frequency, self.f_ref]:
            label.contextMenuEvent = lambda ev: self.menu.popup(
                QtGui.QCursor.pos())
            label.mouseReleaseEvent = lambda ev: None

        # layout GUI
        self.layout.addItem(self.osa, colspan=2)
        self.layout.nextRow()
        self.layout.addItem(self.detuning, colspan=2)
        self.layout.nextRow()
        self.layout.addItem(self.name)
        self.layout.addItem(self.frequency)

        self.dock.addWidget(self.layout, colspan=7)

        self.dock.addWidget(self.fast_mode, row=1, col=1)
        self.dock.addWidget(self.auto_exposure, row=2, col=1)

        self.dock.addWidget(QtGui.QLabel("Reference"), row=1, col=2)
        self.dock.addWidget(QtGui.QLabel("Exposure 0"), row=1, col=3)
        self.dock.addWidget(QtGui.QLabel("Exposure 1"), row=1, col=4)
        self.dock.addWidget(QtGui.QLabel("Lock status"), row=1, col=5)

        self.dock.addWidget(self.f_ref, row=2, col=2)
        self.dock.addWidget(self.exposure[0], row=2, col=3)
        self.dock.addWidget(self.exposure[1], row=2, col=4)
        self.dock.addWidget(self.lock_status, row=2, col=5)

        # Sort the layout to make the most of available space
        self.layout.ci.setSpacing(4)
        self.layout.ci.setContentsMargins(2, 2, 2, 2)
        self.dock.layout.setContentsMargins(0, 0, 0, 4)
        for i in [0, 6]:
            self.dock.layout.setColumnMinimumWidth(i, 4)
        for i in [2, 3, 4, 5]:
            self.dock.layout.setColumnStretch(i, 1)

        self.cb_queue = []

        def add_async_cb(data):
            self.cb_queue.append(data)
            self.wake_loop.set()

        self.ref_editable.triggered.connect(self.ref_editable_cb)
        self.fast_mode.clicked.connect(functools.partial(add_async_cb,
                                                         ("fast_mode",)))
        self.auto_exposure.clicked.connect(functools.partial(add_async_cb,
                                                             ("auto_expose",)))
        self.f_ref.valueChanged.connect(functools.partial(add_async_cb,
                                                          ("f_ref",)))
        for ccd, exp in enumerate(self.exposure):
            exp.valueChanged.connect(functools.partial(add_async_cb,
                                                       ("exposure", ccd)))

        self.fut = asyncio.ensure_future(self.loop())
        self._gui.loop.run_until_complete(self.setConnected(False))

    async def setConnected(self, connected):
        """ Enable/disable controls on the gui dependent on whether or not
        the server is connected.
        """
        if not connected:
            self.ref_editable.blockSignals(True)
            self.fast_mode.blockSignals(True)
            self.auto_exposure.blockSignals(True)
            self.f_ref.blockSignals(True)

            self.ref_editable.setEnabled(False)
            self.fast_mode.setEnabled(False)
            self.auto_exposure.setEnabled(False)
            self.f_ref.setEnabled(False)

            for exp in self.exposure:
                exp.blockSignals(True)
                exp.setEnabled(False)

            self.lock_status.setText("no connection")
            self.lock_status.setStyleSheet("color: red")
        else:
            if self._gui.laser_db[self.laser]["osa"] == "blue":
                self.colour = "5555ff"
            elif self._gui.laser_db[self.laser]["osa"] == "red":
                self.colour = "ff5555"
            else:
                self.colour = "7c7c7c"

            self.name.setText(self.display_name,
                              color=self.colour,
                              size="32pt")

            for ccd, exposure in enumerate(self.exposure):
                exp_min = await self.client.get_min_exposure()
                exp_max = await self.client.get_max_exposure()
                exposure.setRange(exp_min, exp_max)
                exposure.setValue(
                    self._gui.laser_db[self.laser]["exposure"][ccd])

            # sync GUI with server
            self.update_fast_mode()
            self.update_auto_exposure()
            self.update_reference()
            self.update_exposure()
            self.update_lock_status()
            self.update_osa_trace()

            # re-enable GUI controls
            self.ref_editable.setEnabled(True)
            self.fast_mode.setEnabled(True)
            self.auto_exposure.setEnabled(True)
            self.ref_editable_cb()

            self.ref_editable.blockSignals(False)
            self.fast_mode.blockSignals(False)
            self.auto_exposure.blockSignals(False)
            self.f_ref.blockSignals(False)

            for exp in self.exposure:
                exp.setEnabled(True)
                exp.blockSignals(False)

    async def loop(self):
        """ Update task for this laser display.

        Runs as long as the parent window's exit request is not set.

        The loop sleeps until it has something to do. It wakes up when (a) the
        wake_loop event is set (b) a measurement is due.
        """
        laser = self.laser

        while not self._gui.win.exit_request.is_set():
            self.wake_loop.clear()

            try:
                self.client.close_rpc()
            except Exception:
                pass

            if not self.server:
                await self.setConnected(False)
                await self.wake_loop.wait()
                continue

            try:
                server_cfg = self._gui.config["servers"][self.server]
                self.client = RPCClient()
                await self.client.connect_rpc(server_cfg["host"],
                                              server_cfg["control"],
                                              target_name="control")
                await self.setConnected(True)
            # to do: cathch specific exceptions
            except Exception as e:
                logger.error("Error connecting to server '{}' {}"
                             .format(self.server, e))
                continue

            while not self._gui.win.exit_request.is_set() and self.server:
                self.wake_loop.clear()

                try:
                    # process any callbacks
                    while self.cb_queue:
                        next_cb = self.cb_queue[0]
                        if next_cb[0] == "fast_mode":
                            await self.fast_mode_cb()
                        if next_cb[0] == "auto_expose":
                            await self.auto_expose_cb()
                        elif next_cb[0] == "f_ref":
                            await self.f_ref_cb()
                        elif next_cb[0] == "exposure":
                            await self.exposure_cb(next_cb[1])
                        del self.cb_queue[0]

                    # ask for new data
                    if self.fast_mode.isChecked():
                        poll_time = self._gui.args.poll_time_fast
                        priority = FAST_MODE_PRIORITY
                    else:
                        poll_time = self._gui.args.poll_time
                        priority = REGULAR_UPDATE_PRIORITY

                    data_timestamp = min(self._gui.freq_db[laser]["timestamp"],
                                         self._gui.osa_db[laser]["timestamp"])

                    data_expiry = data_timestamp + poll_time
                    next_measurement_in = data_expiry - time.time()
                    if next_measurement_in <= 0:
                        await self.client.get_freq(laser=laser,
                                                   age=poll_time,
                                                   priority=priority,
                                                   get_osa_trace=True,
                                                   blocking=True,
                                                   mute=True)
                        next_measurement_in = poll_time

                except Exception:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    await asyncio.wait_for(self.wake_loop.wait(),
                                           next_measurement_in)
                except asyncio.TimeoutError:
                    pass

    async def fast_mode_cb(self):
        await self.client.set_fast_mode(self.laser, self.fast_mode.isChecked())

    async def auto_expose_cb(self):
        await self.client.set_auto_exposure(self.laser,
                                            self.auto_exposure.isChecked())

    def ref_editable_cb(self):
        """ Enable/disable editing of the frequency reference """
        if not self.ref_editable.isChecked():
            self.f_ref.setEnabled(False)
        else:
            self.f_ref.setEnabled(True)

    async def f_ref_cb(self):
        await self.client.set_reference_freq(self.laser,
                                             self.f_ref.value()*1e12)

    async def exposure_cb(self, ccd):
        await self.client.set_exposure(self.laser,
                                       self.exposure[ccd].value(),
                                       ccd)

    def update_fast_mode(self):
        server_fast_mode = self._gui.laser_db[self.laser]["fast_mode"]
        self.fast_mode.setChecked(server_fast_mode)

    def update_auto_exposure(self):
        server_auto_exposure = self._gui.laser_db[self.laser]["auto_exposure"]
        self.auto_exposure.setChecked(server_auto_exposure)

    def update_exposure(self):
        for ccd, exp in enumerate(self._gui.laser_db[self.laser]["exposure"]):
            self.exposure[ccd].setValue(exp)

    def update_reference(self):
        self.f_ref.setValue(self._gui.laser_db[self.laser]["f_ref"]/1e12)
        self.update_freq()

    def update_osa_trace(self):
        if self._gui.osa_db[self.laser]["trace"] is None:
            return

        trace = np.array(self._gui.osa_db[self.laser]["trace"])/32767
        self.osa_curve.setData(trace)

        self.wake_loop.set()  # recalculate when next measurement due

    def update_freq(self):

        freq = self._gui.freq_db[self.laser]["freq"]
        status = self._gui.freq_db[self.laser]["status"]

        if status == WLMMeasurementStatus.OKAY:
            colour = self.colour
            f_ref = self._gui.laser_db[self.laser]["f_ref"]

            # this happens if the server hasn't taken a measurement yet
            if freq is None:
                return

            if abs(freq - f_ref) > 100e9:
                detuning = "-"
            else:
                detuning = "{:.1f}".format((freq-f_ref)/1e6)
            freq = "{:.7f} THz".format(freq/1e12)
        elif status == WLMMeasurementStatus.UNDER_EXPOSED:
            freq = "-"
            detuning = "Low"
            colour = "ff9900"
        elif status == WLMMeasurementStatus.OVER_EXPOSED:
            freq = "-"
            detuning = "High"
            colour = "ff9900"
        else:
            freq = "-"
            detuning = "Error"
            colour = "ff9900"

        self.frequency.setText(freq)
        self.detuning.setText(detuning, color=colour)

        self.wake_loop.set()  # recalculate when next measurement due

    def update_lock_status(self):
        locked = self._gui.laser_db[self.laser]["locked"]
        owner = self._gui.laser_db[self.laser]["lock_owner"]

        if not locked:
            self.lock_status.setText("unlocked")
            self.lock_status.setStyleSheet("color: grey")
        elif owner:
            self.lock_status.setText("locked by: {}".format(owner))
            self.lock_status.setStyleSheet("color: red")
        else:
            self.lock_status.setText("locked")
            self.lock_status.setStyleSheet("color: orange")