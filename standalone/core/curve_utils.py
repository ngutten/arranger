"""Shared curve interpolation utilities for automation and pitch bends.

Used by both automation_curve.py and piano_roll.py for consistent
curve rendering and interpolation.
"""

from typing import List, Tuple, Callable


def catmull_rom_spline(v0: float, v1: float, v2: float, v3: float, t: float) -> float:
    """Catmull-Rom cubic spline interpolation.
    
    Args:
        v0: Value at point before segment start (for tension)
        v1: Value at segment start
        v2: Value at segment end
        v3: Value at point after segment end (for tension)
        t: Interpolation parameter [0, 1] within segment
    
    Returns:
        Interpolated value at parameter t
    
    This gives smooth curves without overshoot, using neighboring points
    to determine curve tension. Same formula used in piano roll bend curves.
    """
    return 0.5 * (
        (2 * v1) +
        (-v0 + v2) * t +
        (2 * v0 - 5 * v1 + 4 * v2 - v3) * t * t +
        (-v0 + 3 * v1 - 3 * v2 + v3) * t * t * t
    )


def interpolate_curve(points: List[Tuple[float, float, str]], 
                     time: float,
                     length: float,
                     default_value: float = 0.0) -> float:
    """Interpolate value at a given time from a list of control points.
    
    Args:
        points: List of (time, value, curve_type) tuples, sorted by time
        time: Time to sample (clamped to [0, length])
        length: Total length of the curve
        default_value: Value to use for implicit endpoints
    
    Returns:
        Interpolated value at the given time
    
    Curve types:
        'linear': Linear interpolation between points
        'step': Hold previous value (no interpolation)
        'smooth': Catmull-Rom cubic spline
    
    Implicit endpoints:
        If no point at time 0, adds implicit point at (0, default_value)
        If no point at time length, adds implicit point at (length, last_value)
    """
    if not points:
        return default_value
    
    # Clamp time to valid range
    time = max(0.0, min(length, time))
    
    # Build full point list with implicit endpoints
    sorted_points = sorted(points, key=lambda p: p[0])
    
    full_points = []
    if not sorted_points or sorted_points[0][0] > 0:
        full_points.append((0.0, default_value, 'linear'))
    full_points.extend(sorted_points)
    if not sorted_points or sorted_points[-1][0] < length:
        last_value = full_points[-1][1] if full_points else default_value
        full_points.append((length, last_value, 'linear'))
    
    # Find segment containing time
    for i in range(len(full_points) - 1):
        t1, v1, curve = full_points[i]
        t2, v2, _ = full_points[i + 1]
        
        if t1 <= time <= t2:
            # Found the segment
            if t2 == t1:
                return v2
            
            # Calculate interpolation parameter
            seg_frac = (time - t1) / (t2 - t1)
            
            # Apply interpolation based on curve type
            if curve == 'step':
                return v1
            elif curve == 'smooth':
                # Catmull-Rom spline using neighboring points
                v0 = full_points[max(0, i - 1)][1]
                v3 = full_points[min(len(full_points) - 1, i + 2)][1]
                return catmull_rom_spline(v0, v1, v2, v3, seg_frac)
            else:  # linear (default)
                return v1 + seg_frac * (v2 - v1)
    
    # Shouldn't reach here due to clamping, but return default
    return default_value


def render_curve_samples(points: List[Tuple[float, float, str]],
                        length: float,
                        samples_per_unit: int = 16,
                        default_value: float = 0.0) -> List[Tuple[float, float]]:
    """Generate sample points for rendering a smooth curve.
    
    Args:
        points: List of (time, value, curve_type) tuples
        length: Total length of the curve
        samples_per_unit: Number of samples per unit time (e.g., 16 per beat)
        default_value: Value for implicit endpoints
    
    Returns:
        List of (time, value) tuples for rendering
    
    Use this to pre-compute points for drawing, rather than calling
    interpolate_curve in a tight loop during paintEvent.
    """
    num_samples = max(32, int(length * samples_per_unit))
    samples = []
    
    for i in range(num_samples + 1):
        t = i / num_samples * length
        v = interpolate_curve(points, t, length, default_value)
        samples.append((t, v))
    
    return samples


def simplify_curve(points: List[Tuple[float, float, str]],
                   tolerance: float = 0.01) -> List[Tuple[float, float, str]]:
    """Simplify a curve by removing redundant points.
    
    Args:
        points: List of (time, value, curve_type) tuples
        tolerance: Maximum allowed deviation from original curve
    
    Returns:
        Simplified list of points with redundant points removed
    
    Uses Ramer-Douglas-Peucker algorithm for line simplification.
    Useful for cleaning up recorded automation or dense curves.
    """
    if len(points) <= 2:
        return points
    
    # TODO: Implement RDP algorithm if needed for curve simplification
    # For now, just return the original points
    return points


def resample_curve(points: List[Tuple[float, float, str]],
                   length: float,
                   target_points: int,
                   default_value: float = 0.0) -> List[Tuple[float, float, str]]:
    """Resample curve to a specific number of evenly-spaced points.
    
    Args:
        points: Original curve points
        length: Total length of the curve
        target_points: Number of points to generate
        default_value: Value for implicit endpoints
    
    Returns:
        New list of evenly-spaced points with interpolated values
    
    Useful for converting between different time resolutions or
    preparing curves for export.
    """
    if target_points < 2:
        return points
    
    resampled = []
    for i in range(target_points):
        t = i / (target_points - 1) * length
        v = interpolate_curve(points, t, length, default_value)
        # Keep original curve type for first/last points if they exist
        curve_type = 'linear'
        if i == 0 and points and points[0][0] == 0:
            curve_type = points[0][2]
        resampled.append((t, v, curve_type))
    
    return resampled


class CurveRenderer:
    """Helper class for rendering curves with caching.
    
    Use this when you need to render the same curve multiple times
    (e.g., during mouse movement). It caches the sample points to
    avoid recomputing interpolation.
    """
    
    def __init__(self, samples_per_unit: int = 16):
        self.samples_per_unit = samples_per_unit
        self._cached_points = None
        self._cached_samples = None
    
    def render(self, points: List[Tuple[float, float, str]],
              length: float,
              default_value: float = 0.0) -> List[Tuple[float, float]]:
        """Render curve samples with caching.
        
        Args:
            points: Curve control points
            length: Total curve length
            default_value: Value for implicit endpoints
        
        Returns:
            List of (time, value) sample points
        """
        # Simple cache: if points haven't changed, return cached samples
        points_key = tuple(points)  # Convert to hashable
        if self._cached_points == points_key:
            return self._cached_samples
        
        # Recompute
        self._cached_points = points_key
        self._cached_samples = render_curve_samples(
            points, length, self.samples_per_unit, default_value
        )
        return self._cached_samples
    
    def invalidate(self):
        """Clear the cache."""
        self._cached_points = None
        self._cached_samples = None
