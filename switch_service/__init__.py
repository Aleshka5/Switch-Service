# SPDX-License-Identifier: Apache-2.0
"""Control plane for inference services.

States form a triangle: off, cpu and gpu. From any vertex the other two are
the legal targets. RAM and VRAM are reserved by claim so two starts cannot
both be admitted on the same free byte.
"""

from switch_service.core import SwitchCore
from switch_service.errors import SwitchError

__version__ = "0.1.0"
__all__ = ["SwitchCore", "SwitchError", "__version__"]
