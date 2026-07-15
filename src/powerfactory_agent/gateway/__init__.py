"""Vendor-free gateway contract, fake, and structured failures."""

from .errors import *
from .fake import DeterministicFakeGateway, DeterministicHeadlineHarness
from .primitive_fake import DeterministicPrimitiveGateway
from .protocol import PowerFactoryGateway
from .worker import (
    EngineQuarantinedError,
    KnownOperationFailure,
    OperationHandler,
    OperationRequest,
    QueueCapacityError,
    SerializedOperationWorker,
    UnknownOperationHandlerError,
    WorkerAdmissionError,
    WorkerClosedError,
)

__all__ = [name for name in globals() if not name.startswith("_")]
