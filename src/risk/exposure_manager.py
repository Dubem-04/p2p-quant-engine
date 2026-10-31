"""
Working capital allocation controller and dispute lockup circuit breaker.

Enforces strict risk limits on order sizing to preserve liquidity and protect
float capital. Calculates maximum allowable single-ticket limits relative to
liquid reserves (e.g., <= 15%) and monitors capital tied up in active appeals.
Triggers an automated system halt if dispute capital exceeds risk boundaries.

Classes:
    ExposureManager: Evaluates real-time open exposure against active portfolio caps.
"""
