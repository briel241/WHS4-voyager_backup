#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: VDE-TER Stage 2 Transmitter - Type 92/93 over BPSK (Node A)
# Author: voyager_3
# Description: VDE-TER stage 2 link-layer transmitter using the existing BPSK PHY and a VDE TX Adapter
# GNU Radio version: 3.10.9.2

from PyQt5 import Qt
from gnuradio import qtgui
from gnuradio import blocks
from gnuradio import digital
from gnuradio import gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import uhd
import time
import sip
import vdeter_tx_vde_tx_adapter as vde_tx_adapter  # embedded python block



class vdeter_tx(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "VDE-TER Stage 2 Transmitter - Type 92/93 over BPSK (Node A)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("VDE-TER Stage 2 Transmitter - Type 92/93 over BPSK (Node A)")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("GNU Radio", "vdeter_tx")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)

        ##################################################
        # Variables
        ##################################################
        self.tx_gain = tx_gain = 40
        self.tx_dev = tx_dev = 'type=b200'
        self.tx_ampl = tx_ampl = 0.5
        self.sps = sps = 4
        self.samp_rate = samp_rate = 1e6
        self.excess_bw = excess_bw = 0.35
        self.center_freq = center_freq = 157.2e6
        self.bpsk = bpsk = digital.constellation_bpsk().base()
        self.bpsk.set_npwr(1.0)

        ##################################################
        # Blocks
        ##################################################

        self.vde_tx_adapter = vde_tx_adapter.blk()
        self.usrp_sink = uhd.usrp_sink(
            ",".join((tx_dev, '')),
            uhd.stream_args(
                cpu_format="fc32",
                args='',
                channels=list(range(0,1)),
            ),
            '',
        )
        self.usrp_sink.set_samp_rate(samp_rate)
        self.usrp_sink.set_time_unknown_pps(uhd.time_spec(0))

        self.usrp_sink.set_center_freq(center_freq, 0)
        self.usrp_sink.set_antenna('TX/RX', 0)
        self.usrp_sink.set_gain(tx_gain, 0)
        self.tx_spectrum = qtgui.freq_sink_c(
            2048, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            0, #fc
            samp_rate, #bw
            'Transmit Spectrum', #name
            1,
            None # parent
        )
        self.tx_spectrum.set_update_time(0.1)
        self.tx_spectrum.set_y_axis((-140), 10)
        self.tx_spectrum.set_y_label('Relative Gain', 'dB')
        self.tx_spectrum.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, '')
        self.tx_spectrum.enable_autoscale(True)
        self.tx_spectrum.enable_grid(True)
        self.tx_spectrum.set_fft_average(0.2)
        self.tx_spectrum.enable_axis_labels(True)
        self.tx_spectrum.enable_control_panel(False)
        self.tx_spectrum.set_fft_window_normalized(False)



        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.tx_spectrum.set_line_label(i, "Data {0}".format(i))
            else:
                self.tx_spectrum.set_line_label(i, labels[i])
            self.tx_spectrum.set_line_width(i, widths[i])
            self.tx_spectrum.set_line_color(i, colors[i])
            self.tx_spectrum.set_line_alpha(i, alphas[i])

        self._tx_spectrum_win = sip.wrapinstance(self.tx_spectrum.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._tx_spectrum_win)
        self.modulator = digital.generic_mod(
            constellation=bpsk,
            differential=True,
            samples_per_symbol=sps,
            pre_diff_code=True,
            excess_bw=excess_bw,
            verbose=False,
            log=False,
            truncate=False)
        self.amp = blocks.multiply_const_cc(tx_ampl)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.amp, 0), (self.tx_spectrum, 0))
        self.connect((self.amp, 0), (self.usrp_sink, 0))
        self.connect((self.modulator, 0), (self.amp, 0))
        self.connect((self.vde_tx_adapter, 0), (self.modulator, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("GNU Radio", "vdeter_tx")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_tx_gain(self):
        return self.tx_gain

    def set_tx_gain(self, tx_gain):
        self.tx_gain = tx_gain
        self.usrp_sink.set_gain(self.tx_gain, 0)

    def get_tx_dev(self):
        return self.tx_dev

    def set_tx_dev(self, tx_dev):
        self.tx_dev = tx_dev

    def get_tx_ampl(self):
        return self.tx_ampl

    def set_tx_ampl(self, tx_ampl):
        self.tx_ampl = tx_ampl
        self.amp.set_k(self.tx_ampl)

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.tx_spectrum.set_frequency_range(0, self.samp_rate)
        self.usrp_sink.set_samp_rate(self.samp_rate)

    def get_excess_bw(self):
        return self.excess_bw

    def set_excess_bw(self, excess_bw):
        self.excess_bw = excess_bw

    def get_center_freq(self):
        return self.center_freq

    def set_center_freq(self, center_freq):
        self.center_freq = center_freq
        self.usrp_sink.set_center_freq(self.center_freq, 0)

    def get_bpsk(self):
        return self.bpsk

    def set_bpsk(self, bpsk):
        self.bpsk = bpsk




def main(top_block_cls=vdeter_tx, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()

    tb.start()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
