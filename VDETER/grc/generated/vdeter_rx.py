#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: VDE-TER Stage 2 Receiver - Type 92/93 over BPSK (Node B)
# Author: voyager_3
# Description: VDE-TER stage 2 link-layer receiver using the existing F9A8 BPSK PHY and a VDE RX Adapter
# GNU Radio version: 3.10.9.2

from PyQt5 import Qt
from gnuradio import qtgui
from gnuradio import digital
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
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
import vdeter_rx_vde_rx_adapter as vde_rx_adapter  # embedded python block



class vdeter_rx(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "VDE-TER Stage 2 Receiver - Type 92/93 over BPSK (Node B)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("VDE-TER Stage 2 Receiver - Type 92/93 over BPSK (Node B)")
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

        self.settings = Qt.QSettings("GNU Radio", "vdeter_rx")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)

        ##################################################
        # Variables
        ##################################################
        self.sps = sps = 4
        self.samp_rate = samp_rate = 1e6
        self.rx_gain = rx_gain = 40
        self.rx_dev = rx_dev = 'type=b200'
        self.excess_bw = excess_bw = 0.35
        self.center_freq = center_freq = 157.2e6
        self.bpsk = bpsk = digital.constellation_bpsk().base()
        self.bpsk.set_npwr(1.0)

        ##################################################
        # Blocks
        ##################################################

        self.vde_rx_adapter = vde_rx_adapter.blk(tag_name='preamble_detected')
        self.usrp_source = uhd.usrp_source(
            ",".join((rx_dev, '')),
            uhd.stream_args(
                cpu_format="fc32",
                args='',
                channels=list(range(0,1)),
            ),
        )
        self.usrp_source.set_samp_rate(samp_rate)
        self.usrp_source.set_time_unknown_pps(uhd.time_spec(0))

        self.usrp_source.set_center_freq(center_freq, 0)
        self.usrp_source.set_antenna('RX2', 0)
        self.usrp_source.set_rx_agc(False, 0)
        self.usrp_source.set_gain(rx_gain, 0)
        self.sym_sync = digital.symbol_sync_cc(
            digital.TED_MUELLER_AND_MULLER,
            sps,
            0.045,
            1.0,
            1.0,
            1.5,
            1,
            bpsk,
            digital.IR_MMSE_8TAP,
            128,
            [])
        self.rx_spectrum = qtgui.freq_sink_c(
            2048, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            0, #fc
            samp_rate, #bw
            'Receive Spectrum (check signal presence here first)', #name
            1,
            None # parent
        )
        self.rx_spectrum.set_update_time(0.1)
        self.rx_spectrum.set_y_axis((-140), (-20))
        self.rx_spectrum.set_y_label('Relative Gain', 'dB')
        self.rx_spectrum.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, '')
        self.rx_spectrum.enable_autoscale(True)
        self.rx_spectrum.enable_grid(True)
        self.rx_spectrum.set_fft_average(0.2)
        self.rx_spectrum.enable_axis_labels(True)
        self.rx_spectrum.enable_control_panel(False)
        self.rx_spectrum.set_fft_window_normalized(False)



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
                self.rx_spectrum.set_line_label(i, "Data {0}".format(i))
            else:
                self.rx_spectrum.set_line_label(i, labels[i])
            self.rx_spectrum.set_line_width(i, widths[i])
            self.rx_spectrum.set_line_color(i, colors[i])
            self.rx_spectrum.set_line_alpha(i, alphas[i])

        self._rx_spectrum_win = sip.wrapinstance(self.rx_spectrum.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._rx_spectrum_win)
        self.preamble_correlator = digital.correlate_access_code_tag_bb('1111100110101000', 0, 'preamble_detected')
        self.matched = filter.fir_filter_ccf(
            1,
            firdes.root_raised_cosine(
                1.0,
                samp_rate,
                (samp_rate/sps),
                excess_bw,
                (11*sps+1)))
        self.diff_dec = digital.diff_decoder_bb(2, digital.DIFF_DIFFERENTIAL)
        self.decoder = digital.constellation_decoder_cb(bpsk)
        self.costas = digital.costas_loop_cc(0.0628, 2, False)
        self.const_view = qtgui.const_sink_c(
            1024, #size
            'Constellation (two BPSK clusters indicate lock)', #name
            1, #number of inputs
            None # parent
        )
        self.const_view.set_update_time(0.1)
        self.const_view.set_y_axis((-2), 2)
        self.const_view.set_x_axis((-2), 2)
        self.const_view.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, '')
        self.const_view.enable_autoscale(True)
        self.const_view.enable_grid(True)
        self.const_view.enable_axis_labels(True)


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.const_view.set_line_label(i, "Data {0}".format(i))
            else:
                self.const_view.set_line_label(i, labels[i])
            self.const_view.set_line_width(i, widths[i])
            self.const_view.set_line_color(i, colors[i])
            self.const_view.set_line_style(i, styles[i])
            self.const_view.set_line_marker(i, markers[i])
            self.const_view.set_line_alpha(i, alphas[i])

        self._const_view_win = sip.wrapinstance(self.const_view.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._const_view_win)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.costas, 0), (self.const_view, 0))
        self.connect((self.costas, 0), (self.decoder, 0))
        self.connect((self.decoder, 0), (self.diff_dec, 0))
        self.connect((self.diff_dec, 0), (self.preamble_correlator, 0))
        self.connect((self.matched, 0), (self.sym_sync, 0))
        self.connect((self.preamble_correlator, 0), (self.vde_rx_adapter, 0))
        self.connect((self.sym_sync, 0), (self.costas, 0))
        self.connect((self.usrp_source, 0), (self.matched, 0))
        self.connect((self.usrp_source, 0), (self.rx_spectrum, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("GNU Radio", "vdeter_rx")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps
        self.matched.set_taps(firdes.root_raised_cosine(1.0, self.samp_rate, (self.samp_rate/self.sps), self.excess_bw, (11*self.sps+1)))
        self.sym_sync.set_sps(self.sps)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.matched.set_taps(firdes.root_raised_cosine(1.0, self.samp_rate, (self.samp_rate/self.sps), self.excess_bw, (11*self.sps+1)))
        self.rx_spectrum.set_frequency_range(0, self.samp_rate)
        self.usrp_source.set_samp_rate(self.samp_rate)

    def get_rx_gain(self):
        return self.rx_gain

    def set_rx_gain(self, rx_gain):
        self.rx_gain = rx_gain
        self.usrp_source.set_gain(self.rx_gain, 0)

    def get_rx_dev(self):
        return self.rx_dev

    def set_rx_dev(self, rx_dev):
        self.rx_dev = rx_dev

    def get_excess_bw(self):
        return self.excess_bw

    def set_excess_bw(self, excess_bw):
        self.excess_bw = excess_bw
        self.matched.set_taps(firdes.root_raised_cosine(1.0, self.samp_rate, (self.samp_rate/self.sps), self.excess_bw, (11*self.sps+1)))

    def get_center_freq(self):
        return self.center_freq

    def set_center_freq(self, center_freq):
        self.center_freq = center_freq
        self.usrp_source.set_center_freq(self.center_freq, 0)

    def get_bpsk(self):
        return self.bpsk

    def set_bpsk(self, bpsk):
        self.bpsk = bpsk
        self.decoder.set_constellation(self.bpsk)




def main(top_block_cls=vdeter_rx, options=None):

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
