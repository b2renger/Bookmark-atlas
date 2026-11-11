"""Analysis: text vectors, clustering, projections.

These modules need NumPy. The rest of the app does not, so NumPy is imported
here, lazily, with a message that says what is missing.
"""


def require_numpy():
    try:
        import numpy
    except ImportError:
        raise RuntimeError(
            "This feature needs the NumPy package, which is not installed. "
            "The desktop app includes it; for a source install run: pip install numpy"
        ) from None
    return numpy
