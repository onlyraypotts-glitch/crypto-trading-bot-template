class GuardError(Exception):
    """Raised when a transaction fails the pre-sign guard. Never sign after this."""
    def __init__(self, reasons):
        self.reasons = reasons if isinstance(reasons, list) else [reasons]
        super().__init__("; ".join(self.reasons))
