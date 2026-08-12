import os

from panels.network import Panel as NetworkPanel


class Panel(NetworkPanel):
    """Setup-wizard Wi-Fi panel using the shared asynchronous implementation."""

    show_setup_navigation = True

    def on_back_click(self, widget=None):
        self._screen.show_panel("setup_wizard", _("Choose Language"), remove_all=True)

    def on_next_click(self, widget=None):
        self._screen.setup_init = 0
        self._screen.save_init_step()
        self._screen._ws.klippy.restart_firmware()
        os.system('systemctl restart KlipperScreen.service')
