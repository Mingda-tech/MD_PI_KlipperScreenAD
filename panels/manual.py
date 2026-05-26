import gi
import os
import pathlib
import re

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GdkPixbuf
from ks_includes.screen_panel import ScreenPanel

klipperscreendir = pathlib.Path(__file__).parent.resolve().parent
DEFAULT_MANUAL_MODEL = "MD_AD-F4"
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif')


class Panel(ScreenPanel):
    def __init__(self, screen, title):
        super().__init__(screen, title)

        self.base_path = os.path.join(klipperscreendir, "ks_includes", "locales")
        self.current_lang = self._config.get_main_config().get("language", "en")
        self.printer_model = self.get_printer_model()
        self.folder_path = self.get_manual_path()
        self.image_files = self.load_images()
        self.current_image_index = 0
        self.label = None
        self.image = None
        if self.image_files:
            self.init_ui()

    def get_printer_model(self):
        printer_model_keys = self._get_printer_model_keys()
        manual_models = self._get_available_manual_models()
        for manual_model in manual_models:
            if self._model_keys(manual_model) & printer_model_keys:
                return manual_model

        prefix_matches = []
        for manual_model in manual_models:
            distance = self._prefix_match_distance(self._model_keys(manual_model), printer_model_keys)
            if distance is not None:
                prefix_matches.append((distance, manual_model))
        if prefix_matches:
            return sorted(prefix_matches, key=lambda match: match[0])[0][1]

        return DEFAULT_MANUAL_MODEL

    def get_manual_path(self):
        for lang in self._language_candidates():
            model_path = self._manual_path(lang, self.printer_model)
            if self._folder_has_images(model_path):
                return model_path

        for lang in self._language_candidates():
            default_path = self._manual_path(lang, DEFAULT_MANUAL_MODEL)
            if self._folder_has_images(default_path):
                return default_path

        return self._manual_path("en", DEFAULT_MANUAL_MODEL)

    def _language_candidates(self):
        candidates = [self.current_lang, "en"]
        return [
            lang
            for index, lang in enumerate(candidates)
            if lang and lang not in candidates[:index]
        ]

    def _manual_root(self, lang):
        return os.path.join(self.base_path, lang, "manual")

    def _manual_path(self, lang, model):
        return os.path.join(self._manual_root(lang), model)

    def _get_available_manual_models(self):
        models = []
        seen = set()
        for lang in self._language_candidates():
            manual_root = self._manual_root(lang)
            if not os.path.isdir(manual_root):
                continue
            for entry in os.scandir(manual_root):
                if not entry.is_dir() or not self._folder_has_images(entry.path):
                    continue
                model_key = entry.name.casefold()
                if model_key in seen:
                    continue
                seen.add(model_key)
                models.append(entry.name)
        return models

    def _get_printer_model_keys(self):
        model_names = set()
        if self._printer is not None:
            available_commands = getattr(self._printer, "available_commands", {})
            if isinstance(available_commands, dict):
                model_names.update(available_commands.keys())
            else:
                model_names.update(available_commands)

            get_gcode_macros = getattr(self._printer, "get_gcode_macros", None)
            if callable(get_gcode_macros):
                model_names.update(get_gcode_macros())

            get_hidden_gcode_macros = getattr(self._printer, "get_hidden_gcode_macros", None)
            if callable(get_hidden_gcode_macros):
                model_names.update(get_hidden_gcode_macros())

        model_names.update(filter(None, (
            getattr(self._screen, "connected_printer", None),
            getattr(self._screen, "connecting_to_printer", None),
        )))
        return {
            key
            for model_name in model_names
            for key in self._model_keys(model_name)
        }

    @staticmethod
    def _model_keys(model_name):
        model_key = re.sub(r"[^A-Z0-9]", "", str(model_name).upper())
        keys = {model_key} if model_key else set()
        if model_key.startswith("MD"):
            keys.add(model_key[2:])
        return keys

    @staticmethod
    def _prefix_match_distance(manual_keys, printer_model_keys):
        distances = []
        for manual_key in manual_keys:
            for printer_key in printer_model_keys:
                if len(manual_key) < 4 or len(printer_key) < 4:
                    continue
                if manual_key.startswith(printer_key) or printer_key.startswith(manual_key):
                    distances.append(abs(len(manual_key) - len(printer_key)))
        return min(distances) if distances else None

    @staticmethod
    def _folder_has_images(folder_path):
        if not os.path.isdir(folder_path):
            return False
        return any(
            filename.lower().endswith(IMAGE_EXTENSIONS)
            for filename in os.listdir(folder_path)
        )

    def update_language(self, lang_code):
        self.current_lang = lang_code
        self.printer_model = self.get_printer_model()
        self.folder_path = self.get_manual_path()
        self.image_files = self.load_images()
        self.current_image_index = 0
        if self.image_files:
            if self.image is None or self.label is None:
                self.init_ui()
            else:
                self.update_image()
                self.update_label()

    def init_ui(self):
        grid = self._gtk.HomogeneousGrid()
        back_btn = self._gtk.Button("arrow-left", None, "color1", .66)
        back_btn.connect("clicked", self.on_back_clicked)
        grid.attach(back_btn, 0, 0, 1, 1)

        self.label = Gtk.Label()
        self.update_label()
        grid.attach(self.label, 1, 0, 2, 1)

        next_btn = self._gtk.Button("arrow-right", None, "color1", .66)
        next_btn.connect("clicked", self.on_next_clicked)
        grid.attach(next_btn, 3, 0, 1, 1)

        self.image = Gtk.Image()
        self.update_image()
        grid.attach(self.image, 0, 1, 4, 5)

        self.content.add(grid)

    def load_images(self):
        image_files = []
        if os.path.isdir(self.folder_path):
            for filename in os.listdir(self.folder_path):
                if filename.lower().endswith(IMAGE_EXTENSIONS):
                    image_files.append(os.path.join(self.folder_path, filename))
            image_files.sort(key=self._image_sort_key)
        return image_files

    @staticmethod
    def _image_sort_key(filename):
        return [
            int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", os.path.basename(filename))
        ]

    def update_image(self):
        if self.image_files:
            filename = self.image_files[self.current_image_index]
            new_width = 900
            new_height = 450
            if self._screen.width == 1280 and self._screen.height == 800:
                new_width = 1000
                new_height = 600
            elif self._screen.width == 800 and self._screen.height == 480:
                new_width = 600
                new_height = 320
            scaled_pixbuf = scale_image(filename, new_width, new_height)
            self.image.set_from_pixbuf(scaled_pixbuf)

    def update_label(self):
        total_images = len(self.image_files)
        current_image_num = self.current_image_index + 1
        self.label.set_text(f"Page {current_image_num} of {total_images}")

    def on_back_clicked(self, widget):
        if self.current_image_index > 0:
            self.current_image_index -= 1
            self.update_image()
            self.update_label()

    def on_next_clicked(self, widget):
        if self.current_image_index == len(self.image_files) - 1 and self._screen.setup_init == 1 and self._screen.is_show_manual:
            self._screen.is_show_manual = False
            self._screen.show_panel("setup_wizard", _("Choose Language"), remove_all=True)
        if self.current_image_index < len(self.image_files) - 1:
            self.current_image_index += 1
            self.update_image()
            self.update_label()


def scale_image(filename, new_width, new_height):
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(filename)
    scaled_pixbuf = pixbuf.scale_simple(new_width, new_height, GdkPixbuf.InterpType.BILINEAR)
    return scaled_pixbuf
