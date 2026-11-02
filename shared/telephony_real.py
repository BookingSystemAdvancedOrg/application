"""The real telephony client (46elks, ADR-L; Twilio is the documented
alternative), a skeleton until feature L (phone-agent) builds it: every
method is the port's and raises NotImplementedError, so
TELEPHONY_CLIENT=real fails loudly instead of pretending to work."""

from shared.telephony import Telephony


class RealTelephony(Telephony):
    pass
