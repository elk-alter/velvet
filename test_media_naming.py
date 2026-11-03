import unittest
from pathlib import Path
from media_naming import fallback_title, generic_name
class NamingTests(unittest.TestCase):
 def test_generic_files_use_directory(self):
  for name in ['VIDEO.mp4','video_001.mp4','VID-02.mkv','001.mp4','part1.mp4']:
   self.assertEqual(fallback_title(Path('collection')/'Meaningful Title'/name),'Meaningful Title')
 def test_structural_directories_are_skipped(self):
  self.assertEqual(fallback_title(Path('collection')/'Meaningful Title'/'VIDEO_TS'/'VIDEO.mp4'),'Meaningful Title')
 def test_specific_names_are_retained(self):
  self.assertEqual(fallback_title(Path('Title')/'A real filename.mp4'),'A real filename')
  self.assertFalse(generic_name('Video Games Documentary'))
