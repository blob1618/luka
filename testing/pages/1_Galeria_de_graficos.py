"""Offline visual review of the production chart renderer."""
import streamlit as st

from testing.chart_samples import sample_specs
from app.services.movement_chart import MovementChartService

st.set_page_config(page_title="Galería de gráficos", page_icon="📊")
st.title("Galería de gráficos")
st.caption("Datos de ejemplo. No consume llamadas a proveedores ni modifica tus movimientos.")
samples = sample_specs()
selected = st.selectbox("Ejemplo", list(samples))
png = MovementChartService.render_png(samples[selected])
mobile = st.checkbox("Simular ancho de WhatsApp", value=True)
st.image(png, width=360 if mobile else 680)
st.download_button("Descargar PNG", png, file_name="luka-grafico.png", mime="image/png")
