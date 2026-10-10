(function () {
  if (!window.Chart) {
    document.querySelectorAll('.chart-box').forEach(b => { b.textContent = 'Charts need the Chart.js library, which could not be loaded (no internet access?).'; });
    return;
  }
  // Shade the normal range green and outside it light red, as in BPSXMLViewer.
  Chart.register({
    id: 'rangeBand',
    beforeDraw(chart, _args, opts) {
      const {lower, upper} = opts || {};
      if (lower == null && upper == null) return;
      const {ctx, chartArea: {top, bottom, left, right}, scales: {y}} = chart;
      const Y = v => Math.max(top, Math.min(bottom, y.getPixelForValue(v)));
      ctx.save();
      ctx.fillStyle = 'rgba(46, 125, 50, 0.10)';
      const gTop = upper != null ? Y(upper) : top, gBottom = lower != null ? Y(lower) : bottom;
      ctx.fillRect(left, gTop, right - left, gBottom - gTop);
      ctx.fillStyle = 'rgba(198, 40, 40, 0.07)';
      if (upper != null) ctx.fillRect(left, top, right - left, Y(upper) - top);
      if (lower != null) ctx.fillRect(left, Y(lower), right - left, bottom - Y(lower));
      ctx.restore();
    }
  });
  const colours = ['#1f6feb', '#c2410c', '#15803d', '#7e22ce'];
  window.CHARTS.forEach((c, i) => {
    new Chart(document.getElementById(`chart-${i + 1}`), {
      type: 'line',
      data: {
        labels: c.labels,
        datasets: c.datasets.map((d, j) => ({
          label: d.label, data: d.data, borderColor: colours[j % colours.length], backgroundColor: colours[j % colours.length],
          borderWidth: 2, pointRadius: 3.5, pointHoverRadius: 6, tension: 0.15, spanGaps: true,
        })),
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        scales: {y: {beginAtZero: false, title: {display: !!c.units, text: c.units}}, x: {ticks: {maxRotation: 0, autoSkip: true}}},
        plugins: {legend: {display: c.datasets.length > 1}, rangeBand: {lower: c.range[0], upper: c.range[1]}},
      },
    });
  });
})();
