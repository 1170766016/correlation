import plotly.graph_objects as go

fig = go.Figure(data=[go.Sankey(
    node = dict(
      pad = 15,
      thickness = 20,
      line = dict(color = "black", width = 0.5),
      label = ["A1", "A2", "B1", "B2", "C1", "C2"],
      color = "blue"
    ),
    link = dict(
      source = [0, 1, 0, 2, 3, 3],
      target = [2, 3, 3, 4, 4, 5],
      value =  [8, 4, 2, 8, 4, 2]
  ))])

fig.update_layout(width=800, height=600, title_text="Basic Sankey Diagram", font_size=10)

fig_html = fig.to_html(include_plotlyjs='cdn', full_html=False)

html = f"""
<html>
<head></head>
<body>
<div style="position: relative; width: 100%; height: 100%; overflow: auto;">
    <div style="position: fixed; top: 10px; right: 20px; z-index: 1000; background: white; border: 1px solid #ccc; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.2);">
        <button onclick="zoomSankey(1.2)" style="border:none; background:none; font-size:18px; cursor:pointer; padding: 4px 8px;">➕</button>
        <button onclick="zoomSankey(0.8)" style="border:none; background:none; font-size:18px; cursor:pointer; padding: 4px 8px; border-left: 1px solid #ccc;">➖</button>
    </div>
    <div id="chart-wrapper">
        {fig_html}
    </div>
</div>
<script>
    function zoomSankey(factor) {{
        var plotDiv = document.getElementsByClassName('plotly-graph-div')[0];
        var currentWidth = plotDiv.layout.width || 800;
        var currentHeight = plotDiv.layout.height || 600;
        Plotly.relayout(plotDiv, {{
            width: currentWidth * factor,
            height: currentHeight * factor
        }});
    }}
</script>
</body>
</html>
"""

with open('sankey_test.html', 'w', encoding='utf-8') as f:
    f.write(html)
