import logging
import nodriver
from nodriver import cdp
from ..targets import TargetInterceptor
from ...cdp_helpers import can_use_domain

logger = logging.getLogger(__name__)


async def patch_window_size(
	connection: nodriver.Tab | nodriver.Connection,
	ev: cdp.target.AttachedToTarget | None,
	*,
	width: int,
	height: int,
	device_scale_factor: float = 1.0,
	mobile: bool = False,
	orientation: str | None = None,
):
	"""apply device metrics override if emulation domain is available.

	keeps quiet when emulation is not exposed (workers/etc.). mirrors screen size
	so detection scripts comparing inner/outer sizes see consistent values.

	:param ev: may be None when called directly on a tab with no attach event.
	:param width: viewport width css px.
	:param height: viewport height css px.
	:param device_scale_factor: dpr (usually 1.0 for desktop).
	:param mobile: emulate mobile viewport if True.
	:param orientation: optional orientation (landscapePrimary / portraitPrimary).
	"""

	if isinstance(connection, nodriver.Tab) and ev is None:
		target_type = "tab"
		session_id = None
		msg = f"tab <{connection.url}>"
	else:
		target_type = ev.target_info.type_
		session_id = ev.session_id
		msg = f"{target_type} <{ev.target_info.url}>"

	if not can_use_domain(target_type, "Emulation"):
		logger.debug("skipping window size patch for %s", msg)
		return

	try:
		await connection.send(cdp.emulation.set_device_metrics_override(
			width,
			height,
			device_scale_factor,
			mobile,
			screen_width=width,
			screen_height=height,
			screen_orientation={"type": orientation, "angle": 0} if orientation else None
		), session_id)
	except Exception:
		logger.exception("failed patching window size for %s:", msg)
	else:
		logger.debug(
			"successfully patched window size for %s (width=%d height=%d dpr=%s mobile=%s)",
			msg,
			width,
			height,
			device_scale_factor,
			mobile,
		)


class WindowSizePatch(TargetInterceptor):
	"""stock interceptor for setting viewport metrics via emulation domain.

	skips targets without emulation. mirrors width/height to screen metrics.
	"""

	width: int
	height: int
	device_scale_factor: float
	mobile: bool
	orientation: str | None

	def __init__(
		self,
		width: int,
		height: int,
		*,
		device_scale_factor: float = 1.0,
		mobile: bool = False,
		orientation: str | None = None,
	):
		self.width = width
		self.height = height
		self.device_scale_factor = device_scale_factor
		self.mobile = mobile
		self.orientation = orientation

	async def handle(
		self,
		connection: nodriver.Tab | nodriver.Connection,
		ev: cdp.target.AttachedToTarget | None,
	):
		await patch_window_size(
			connection,
			ev,
			width=self.width,
			height=self.height,
			device_scale_factor=self.device_scale_factor,
			mobile=self.mobile,
			orientation=self.orientation,
		)

