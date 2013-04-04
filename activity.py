# Copyright 2013 Agustin Zubiaga <aguz@sugarlabs.org>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import logging
from gettext import gettext as _

from gi.repository import GObject
GObject.threads_init()
from gi.repository import Gtk
from gi.repository import WebKit

import telepathy
import dbus
import os.path

from sugar3.activity import activity
from sugar3.activity.widgets import ActivityToolbarButton
from sugar3.activity.widgets import StopButton
from sugar3.graphics.toolbarbox import ToolbarBox

import downloadmanager
from filepicker import FilePicker
import server

JOURNAL_STREAM_SERVICE = 'journal-activity-http'

# directory exists if powerd is running.  create a file here,
# named after our pid, to inhibit suspend.
POWERD_INHIBIT_DIR = '/var/run/powerd-inhibit-suspend'

class JournalShare(activity.Activity):

    def __init__(self, handle):

        activity.Activity.__init__(self, handle)

        self.server_proc = None
        self.port = 2500
        if not self.shared_activity:
            activity_path = activity.get_bundle_path()
            activity_root = activity.get_activity_root()
            #TODO: check available port
            server.run_server(activity_path, activity_root, self.port)

        toolbar_box = ToolbarBox()

        activity_button = ActivityToolbarButton(self)
        toolbar_box.toolbar.insert(activity_button, 0)
        activity_button.show()

        separator = Gtk.SeparatorToolItem()
        separator.props.draw = False
        separator.set_expand(True)
        separator.show()
        toolbar_box.toolbar.insert(separator, -1)

        stopbutton = StopButton(self)
        toolbar_box.toolbar.insert(stopbutton, -1)
        stopbutton.show()

        self.set_toolbar_box(toolbar_box)
        toolbar_box.show()

        self.view = WebKit.WebView()
        self.view.connect('mime-type-policy-decision-requested',
                     self.__mime_type_policy_cb)
        self.view.connect('download-requested', self.__download_requested_cb)

        try:
            self.view.connect('run-file-chooser', self.__run_file_chooser)
        except TypeError:
            # Only present in WebKit1 > 1.9.3 and WebKit2
            pass

        self.view.load_html_string('<html><body>Loading...</body></html>',
                'file:///')

        self.view.show()
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(self.view)
        scrolled.show()
        self.set_canvas(scrolled)

        # collaboration
        self.unused_download_tubes = set()
        self.connect("shared", self._shared_cb)

        if self.shared_activity:
            # We're joining
            if self.get_shared():
                # Already joined for some reason, just connect
                self._joined_cb(self)
            else:
                # Wait for a successful join before trying to connect
                self.connect("joined", self._joined_cb)
        else:
            self.view.load_uri('http://0.0.0.0:%d/web/index.html' %
                    self.port)
            # if I am the server
            self._inhibit_suspend()

    def _joined_cb(self, also_self):
        """Callback for when a shared activity is joined.
        Get the shared tube from another participant.
        """
        self.watch_for_tubes()
        GObject.idle_add(self._get_view_information)

    def _get_view_information(self):
        # Pick an arbitrary tube we can try to connect to the server
        try:
            tube_id = self.unused_download_tubes.pop()
        except (ValueError, KeyError), e:
            logging.error('No tubes to connect from right now: %s',
                          e)
            return False

        GObject.idle_add(self._set_view_url, tube_id)
        return False

    def _set_view_url(self, tube_id):
        chan = self.shared_activity.telepathy_tubes_chan
        iface = chan[telepathy.CHANNEL_TYPE_TUBES]
        addr = iface.AcceptStreamTube(tube_id,
                telepathy.SOCKET_ADDRESS_TYPE_IPV4,
                telepathy.SOCKET_ACCESS_CONTROL_LOCALHOST, 0,
                utf8_strings=True)
        logging.error('Accepted stream tube: listening address is %r', addr)
        # SOCKET_ADDRESS_TYPE_IPV4 is defined to have addresses of type '(sq)'
        assert isinstance(addr, dbus.Struct)
        assert len(addr) == 2
        assert isinstance(addr[0], str)
        assert isinstance(addr[1], (int, long))
        assert addr[1] > 0 and addr[1] < 65536
        port = int(addr[1])

        self.view.load_uri('http://%s:%d/web/index.html' % (addr[0], port))
        return False

    def _start_sharing(self):
        """Share the web server."""

        # Make a tube for the web server
        chan = self.shared_activity.telepathy_tubes_chan
        iface = chan[telepathy.CHANNEL_TYPE_TUBES]
        self._fileserver_tube_id = iface.OfferStreamTube(
                JOURNAL_STREAM_SERVICE, {},
                telepathy.SOCKET_ADDRESS_TYPE_IPV4,
                ('127.0.0.1', dbus.UInt16(self.port)),
                telepathy.SOCKET_ACCESS_CONTROL_LOCALHOST, 0)

    def watch_for_tubes(self):
        """Watch for new tubes."""
        if self.server_proc is not None:
            # I am sharing, then, don't try to connect to the tubes
            return

        tubes_chan = self.shared_activity.telepathy_tubes_chan

        tubes_chan[telepathy.CHANNEL_TYPE_TUBES].connect_to_signal('NewTube',
            self._new_tube_cb)
        tubes_chan[telepathy.CHANNEL_TYPE_TUBES].ListTubes(
            reply_handler=self._list_tubes_reply_cb,
            error_handler=self._list_tubes_error_cb)

    def _new_tube_cb(self, tube_id, initiator, tube_type, service, params,
                     state):
        """Callback when a new tube becomes available."""
        logging.error('New tube: ID=%d initator=%d type=%d service=%s '
                      'params=%r state=%d', tube_id, initiator, tube_type,
                      service, params, state)
        if service == JOURNAL_STREAM_SERVICE:
            logging.error('I could download from that tube')
            self.unused_download_tubes.add(tube_id)
            GObject.idle_add(self._get_view_information)

    def _list_tubes_reply_cb(self, tubes):
        """Callback when new tubes are available."""
        for tube_info in tubes:
            self._new_tube_cb(*tube_info)

    def _list_tubes_error_cb(self, e):
        """Handle ListTubes error by logging."""
        logging.error('ListTubes() failed: %s', e)

    def _shared_cb(self, activityid):
        """Callback when activity shared.
        Set up to share the document.
        """
        # We initiated this activity and have now shared it, so by
        # definition the server is local.
        logging.error('Activity became shared')
        self.watch_for_tubes()
        self._start_sharing()

    def __mime_type_policy_cb(self, webview, frame, request, mimetype,
                              policy_decision):
        if not self.view.can_show_mime_type(mimetype):
            policy_decision.download()
            return True

        return False

    def __run_file_chooser(self, browser, request):
        picker = FilePicker(self)
        chosen = picker.run()
        picker.destroy()
        if chosen:
            logging.error('CHOSEN %s', chosen)
            tmp_dir = os.path.dirname(chosen)
            preview_file = os.path.join(tmp_dir, 'preview')
            metadata_file = os.path.join(tmp_dir, 'metadata')
            request.select_files([chosen, preview_file, metadata_file])
        elif hasattr(request, 'cancel'):
            # WebKit2 only
            request.cancel()
        return True

    def __download_requested_cb(self, browser, download):
        downloadmanager.add_download(download, browser)
        return True

    def read_file(self, file_path):
        pass

    def write_file(self, file_path):
        pass

    def can_close(self):
        if self.server_proc is not None:
            self.server_proc.kill()
        self._allow_suspend()
        return True

    # power management (almost copied from clock activity)

    def powerd_running(self):
        return os.access(POWERD_INHIBIT_DIR, os.W_OK)

    def _inhibit_suspend(self):
        if self.powerd_running():
            fd = open(POWERD_INHIBIT_DIR + "/%u" % os.getpid(), 'w')
            fd.close()
            return True
        else:
            return False

    def _allow_suspend(self):
        if self.powerd_running():
            if os.path.exists(POWERD_INHIBIT_DIR + "/%u" % os.getpid()):
                os.unlink(POWERD_INHIBIT_DIR + "/%u" % os.getpid())
            return True
        else:
            return False
