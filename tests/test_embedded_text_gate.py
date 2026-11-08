from pathlib import Path
import unittest
from PIL import Image, ImageDraw, ImageFont
from imagegen.worker import Worker


class EmbeddedTextGateTests(unittest.TestCase):
    def canvas(self):
        return Image.new('RGB', (1024, 576), (45, 60, 50))

    def font(self, size=28):
        for name in ('arial.ttf', 'DejaVuSans.ttf'):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass
        return ImageFont.load_default(size=size)

    def test_white_subtitles_remain_rejected(self):
        for text in ('Winter is coming. We must leave now.',
                     'Зима близко. Нам нужно уходить.'):
            with self.subTest(text=text):
                image = self.canvas()
                ImageDraw.Draw(image).text((180, 505), text, font=self.font(), fill='white')
                self.assertTrue(Worker._has_embedded_text(image))

    def test_coloured_subtitle_remains_rejected(self):
        image = self.canvas()
        ImageDraw.Draw(image).text((145, 500), 'IF YOU SEE YOURSELF - DO NOT LET HIM ENTER',
                                  fill=(165, 225, 245), font=self.font(30))
        self.assertTrue(Worker._has_embedded_text(image))

    def test_floating_words_remain_rejected(self):
        image = self.canvas()
        ImageDraw.Draw(image).text(
            (130, 230), 'IF YOU HEAR A STREAM DO NOT SEEK IT KEEP WALKING AWAY',
            fill=(185,210,205), font=self.font(30))
        self.assertTrue(Worker._has_embedded_text(image))

    def test_speech_bubble_remains_rejected(self):
        image = Image.new('RGB', (1024, 576), (150, 170, 160))
        draw = ImageDraw.Draw(image)
        draw.ellipse((200, 90, 650, 330), fill='white', outline='black', width=3)
        draw.polygon(((300, 305), (280, 355), (360, 305)), fill='white')
        draw.text((265, 175), 'We must leave now!', fill='black', font=self.font())
        self.assertTrue(Worker._has_embedded_text(image))

    def test_bright_snow_with_ink_strokes_is_allowed(self):
        image = Image.new('RGB', (1024, 576), (221, 232, 235))
        crop = Image.open(Path(__file__).parent / 'fixtures/caption_snow_strokes.png')
        image.paste(crop, (0, 430))
        self.assertFalse(Worker._has_embedded_text(image))

    def test_small_physical_sign_is_allowed(self):
        image = self.canvas()
        draw = ImageDraw.Draw(image)
        draw.rectangle((700, 90, 920, 155), fill=(125, 84, 42))
        draw.text((715, 105), 'NORTHWOOD', fill=(245, 235, 205), font=self.font(20))
        self.assertFalse(Worker._has_embedded_text(image))

    def test_plain_pale_surfaces_are_allowed(self):
        image = self.canvas()
        draw = ImageDraw.Draw(image)
        draw.rectangle((130, 460, 550, 510), fill=(240, 243, 245))
        self.assertFalse(Worker._has_embedded_text(image))

    def test_near_stone_highlights_are_allowed(self):
        image = self.canvas()
        with Image.open(Path(__file__).parent / 'fixtures/riverbank_stone_highlights.png') as crop:
            image.paste(crop, (170, 200))
        self.assertFalse(Worker._has_embedded_text(image))

    def test_moss_highlights_are_allowed(self):
        image = self.canvas()
        with Image.open(Path(__file__).parent / 'fixtures/moss_stone_highlights.png') as crop:
            image.paste(crop, (210, 390))
        self.assertFalse(Worker._has_embedded_text(image))


if __name__ == '__main__':
    unittest.main()
