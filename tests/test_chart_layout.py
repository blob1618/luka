"""Layout regressions at the actual export size, including long labels."""
import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg

from testing.chart_samples import sample_specs
from app.services.movement_chart import MovementChartService


@pytest.mark.parametrize("spec", list(sample_specs().values()))
def test_labels_stay_inside_canvas_and_do_not_overlap(spec):
    figure = MovementChartService.build_figure(spec)
    try:
        canvas = FigureCanvasAgg(figure)
        canvas.draw()
        renderer = canvas.get_renderer()
        bounds = figure.bbox
        texts = [text for axis in figure.axes for text in axis.texts if text.get_text()]
        boxes = [text.get_window_extent(renderer) for text in texts]
        for box in boxes:
            assert box.x0 >= bounds.x0 and box.x1 <= bounds.x1
            assert box.y0 >= bounds.y0 and box.y1 <= bounds.y1
        for index, box in enumerate(boxes):
            for other_index in range(index + 1, len(boxes)):
                assert not box.overlaps(boxes[other_index]), (texts[index].get_text(), texts[other_index].get_text())
    finally:
        figure.clear()
