"""Subprocess-only native lifecycle probe. Never logs in or submits orders."""

import ctypes
import faulthandler
import gc
import json
import sys


def main():
    faulthandler.enable()
    import wx

    from tfx_quant.infrastructure.yuanta.quote_com_host import YuantaQuoteComHost

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetProcessHeaps.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    kernel.GetProcessHeaps.restype = ctypes.c_uint32
    kernel.HeapValidate.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    kernel.HeapValidate.restype = ctypes.c_int
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindow.argtypes = [ctypes.c_void_p]
    user.IsWindow.restype = ctypes.c_int

    def checkpoint(phase, cycle):
        count = kernel.GetProcessHeaps(0, None)
        heaps = (ctypes.c_void_p * count)()
        actual = kernel.GetProcessHeaps(count, heaps)
        if not 0 < actual <= count:
            raise RuntimeError("Process heap list changed during capture")
        invalid = [hex(heap) for heap in heaps[:actual] if not kernel.HeapValidate(heap, 0, None)]
        print(
            json.dumps({"phase": phase, "cycle": cycle, "heaps": actual, "invalid_heaps": invalid}),
            flush=True,
        )
        if invalid:
            raise RuntimeError("HeapValidate detected an invalid heap")

    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activator = wx.EventLoopActivator(loop)
    owner = wx.Frame(None, title="Hidden quote lifecycle probe")
    app.SetTopWindow(owner)
    try:
        for cycle in range(int(sys.argv[1])):
            print(json.dumps({"phase": "creating", "cycle": cycle}), flush=True)
            host = YuantaQuoteComHost(lambda _: None, lambda _: None, parent=owner)
            hwnd = host._frame.GetHandle()
            checkpoint("created", cycle)
            host.close()
            host.close()  # Idempotency while wx destruction is still pending.
            checkpoint("close_requested", cycle)
            app.Yield(True)
            loop.ProcessIdle()
            if user.IsWindow(hwnd):
                raise RuntimeError("Closed host's native window was not destroyed")
            del host
            gc.collect()
            app.Yield(True)
            loop.ProcessIdle()
            checkpoint("collected", cycle)
        print("PROBE_OK", flush=True)
    finally:
        owner.Destroy()
        app.Yield(True)
        loop.ProcessIdle()
        del activator


if __name__ == "__main__":
    main()
