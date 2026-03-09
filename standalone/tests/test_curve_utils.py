"""Tests for curve utility functions."""

import pytest
from standalone.core.curve_utils import (
    simplify_curve, interpolate_curve, catmull_rom_spline,
)


class TestSimplifyCurve:
    def test_empty(self):
        assert simplify_curve([]) == []

    def test_two_points(self):
        pts = [(0, 0, 'linear'), (1, 1, 'linear')]
        assert simplify_curve(pts) == pts

    def test_collinear_points_simplified(self):
        """Three collinear points should reduce to two."""
        pts = [(0, 0, 'linear'), (0.5, 0.5, 'linear'), (1, 1, 'linear')]
        result = simplify_curve(pts, tolerance=0.01)
        assert len(result) == 2
        assert result[0] == pts[0]
        assert result[-1] == pts[-1]

    def test_significant_point_kept(self):
        """A point far from the line should be preserved."""
        pts = [(0, 0, 'linear'), (0.5, 1.0, 'linear'), (1, 0, 'linear')]
        result = simplify_curve(pts, tolerance=0.01)
        assert len(result) == 3

    def test_dense_sine_simplified(self):
        """A densely sampled curve should be simplified significantly."""
        import math
        pts = [(i / 100, math.sin(i / 100 * math.pi), 'linear')
               for i in range(101)]
        result = simplify_curve(pts, tolerance=0.01)
        # Should keep far fewer points than 101 but more than 2
        assert 2 < len(result) < 50

    def test_preserves_curve_type(self):
        pts = [(0, 0, 'smooth'), (0.5, 1.0, 'step'), (1, 0, 'linear')]
        result = simplify_curve(pts, tolerance=0.001)
        # All three should be kept (non-collinear)
        assert len(result) == 3
        assert result[1][2] == 'step'


class TestInterpolateCurve:
    def test_empty(self):
        assert interpolate_curve([], 0.5, 1.0) == 0.0

    def test_linear(self):
        pts = [(0.0, 0.0, 'linear'), (1.0, 1.0, 'linear')]
        assert abs(interpolate_curve(pts, 0.5, 1.0) - 0.5) < 0.01

    def test_step(self):
        pts = [(0.0, 0.0, 'step'), (1.0, 1.0, 'step')]
        # Step holds previous value throughout the segment
        assert interpolate_curve(pts, 0.5, 1.0) == 0.0
        assert interpolate_curve(pts, 0.99, 1.0) == 0.0

    def test_clamps_to_range(self):
        pts = [(0.0, 0.5, 'linear'), (1.0, 0.5, 'linear')]
        assert interpolate_curve(pts, -1.0, 1.0) == 0.5
        assert interpolate_curve(pts, 2.0, 1.0) == 0.5


class TestCatmullRom:
    def test_midpoint(self):
        # At t=0.5 between v1=0 and v2=1 with flat neighbors
        result = catmull_rom_spline(0, 0, 1, 1, 0.5)
        assert abs(result - 0.5) < 0.01

    def test_endpoints(self):
        assert abs(catmull_rom_spline(0, 1, 2, 3, 0.0) - 1.0) < 1e-6
        assert abs(catmull_rom_spline(0, 1, 2, 3, 1.0) - 2.0) < 1e-6
