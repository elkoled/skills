# run with the checkout's python: clip_runner.py OVERLAY_DIR CLIP_SCRIPT [clip args...]
# runs the checkout's tools/clip/run.py, adding what an older clip tool lacks so every render is fast
# and deterministic:
#   - streaming decode, one ffmpeg per segment instead of one per GOP (about 20x faster decode)
#   - frame clock, UI timers follow the frame index instead of the wall clock
# OVERLAY_DIR (may be empty) holds <dotted.module>.py files imported in place of the checkout's
# copy, so a git ref's UI code can be rendered. stdlib only besides what run.py already imports
import importlib.abc
import importlib.util
import os
import subprocess
import sys
import threading
import time


class OverlayFinder(importlib.abc.MetaPathFinder):
  def __init__(self, overlay_dir: str):
    self.overlay_dir = overlay_dir

  def find_spec(self, name, path, target=None):
    fn = os.path.join(self.overlay_dir, name + ".py")
    if os.path.exists(fn):
      return importlib.util.spec_from_file_location(name, fn)
    return None


def stream_segment(path: str, frame_size: int, use_qcam: bool):
  from openpilot.tools.lib.filereader import FileReader
  in_args = [] if use_qcam else ["-c:v", "hevc", "-vsync", "0", "-f", "hevc", "-flags2", "showall"]
  with FileReader(path) as f:
    data = f.read()
  proc = subprocess.Popen(["ffmpeg", "-v", "quiet", "-hwaccel", "auto", *in_args, "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "nv12", "-"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE)

  def feed():
    try:
      proc.stdin.write(data)
      proc.stdin.close()
    except BrokenPipeError:
      pass
  threading.Thread(target=feed, daemon=True).start()

  try:
    while len(frame := proc.stdout.read(frame_size)) == frame_size:
      yield frame
  finally:
    proc.kill()
    proc.wait()


def make_iter_segment_frames(clip):
  import numpy as np

  # same contract as the old iter_segment_frames, it yields arrays the FrameQueue calls .tobytes() on
  def iter_segment_frames(camera_paths, start_time, end_time, fps=20, use_qcam=False, frame_size=None):
    frames_per_seg = fps * 60
    current_seg, frames, next_local_idx, frame = -1, None, 0, None
    for global_idx in range(int(start_time * fps), int(end_time * fps)):
      seg_idx, local_idx = divmod(global_idx, frames_per_seg)
      if seg_idx != current_seg:
        current_seg = seg_idx
        path = camera_paths[seg_idx] if seg_idx < len(camera_paths) else None
        if not path:
          raise RuntimeError(f"No camera file for segment {seg_idx}")
        w, h = frame_size or clip.get_frame_dimensions(path)
        frames, next_local_idx = stream_segment(path, w * h * 3 // 2, use_qcam), 0
      while next_local_idx <= local_idx:
        frame = next(frames, None)
        if frame is None:
          raise RuntimeError(f"Ran out of frames in segment {seg_idx} at frame {next_local_idx}")
        next_local_idx += 1
      yield global_idx, np.frombuffer(frame, dtype=np.uint8)
  return iter_segment_frames


# patch_submaster runs inside clip() after the UI imports, so the RECORD env is already set up.
# wrap gui_app.render there to drive time.monotonic and rl.get_time from the rendered frame count
def add_frame_clock(clip):
  orig_patch_submaster = clip.patch_submaster

  def patch_submaster(message_chunks, ui_state):
    orig_patch_submaster(message_chunks, ui_state)
    import pyray as rl
    from openpilot.system.ui.lib.application import gui_app
    orig_render = gui_app.render

    def render():
      frame = 0
      real_monotonic, real_get_time = time.monotonic, rl.get_time
      t0 = real_monotonic()
      time.monotonic, rl.get_time = lambda: t0 + frame / clip.FRAMERATE, lambda: frame / clip.FRAMERATE
      ui_state.started_time = time.monotonic()
      try:
        for item in orig_render():
          yield item
          frame += 1
      finally:
        time.monotonic, rl.get_time = real_monotonic, real_get_time
    gui_app.render = render
  clip.patch_submaster = patch_submaster


def main() -> None:
  overlay_dir, script = sys.argv[1], sys.argv[2]
  if overlay_dir:
    sys.meta_path.insert(0, OverlayFinder(overlay_dir))
  sys.argv = sys.argv[2:]

  spec = importlib.util.spec_from_file_location("clip_run", script)
  clip = importlib.util.module_from_spec(spec)
  sys.modules["clip_run"] = clip  # multiprocessing workers look up functions by module name
  spec.loader.exec_module(clip)

  if not hasattr(clip, "decode_segment"):
    clip.iter_segment_frames = make_iter_segment_frames(clip)
  if not hasattr(clip, "frame_clock"):
    add_frame_clock(clip)
  clip.main()


if __name__ == "__main__":
  main()
