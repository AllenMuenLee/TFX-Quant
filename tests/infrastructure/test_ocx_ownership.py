"""Exercise actual comtypes reference ownership without loading vendor code."""

import ctypes
import gc
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows COM ownership")


def test_atl_output_and_returned_interface_references_are_balanced(monkeypatch):
    import comtypes
    from comtypes.automation import IDispatch

    from tfx_quant.infrastructure.yuanta.ocx_hosting import create_activex_control

    class Object(comtypes.COMObject):
        _com_interfaces_ = [IDispatch]

    container, control = Object(), Object()
    anchors = [obj.QueryInterface(IDispatch) for obj in (container, control)]
    # Extra references let a regression report an assertion instead of freeing
    # the backing object and crashing the test runner on a second Release.
    for anchor in anchors:
        for _ in range(10):
            anchor.AddRef()
    baseline = [obj._refcnt.value for obj in (container, control)]

    def create(_progid, _hwnd, _stream, out_container, out_control, _iid, _sink):
        for obj, output in ((container, out_container), (control, out_control)):
            pointer = obj.QueryInterface(comtypes.IUnknown)
            pointer.AddRef()  # Transfer one reference to ATL's output parameter.
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(
                pointer, ctypes.c_void_p
            ).value
        return 0

    monkeypatch.setattr(ctypes, "OleDLL", lambda _: SimpleNamespace(AtlAxCreateControlEx=create))
    try:
        result = create_activex_control("test", 1, IDispatch)
        gc.collect()
        during = [obj._refcnt.value for obj in (container, control)]
        del result
        gc.collect()
        after = [obj._refcnt.value for obj in (container, control)]
        assert (during, after) == ([baseline[0], baseline[1] + 1], baseline)
    finally:
        for obj, anchor in zip((container, control), anchors, strict=True):
            while obj._refcnt.value > 1:
                anchor.Release()
