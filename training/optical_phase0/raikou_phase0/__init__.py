"""Raikou Optical, Phase 0: India land-cover head on OlmoEarth v1.2 Base.

Modules (heavy dependencies are imported inside functions, so the CPU notebook
never needs torch and the GPU notebooks never need odc-stac until they use it):

  config          every setting (paths, classes, seasons, rules, training)
  tiles           the 100-tile grid, splits and leakage checks
  s2_pc           Sentinel-2 L2A from Planetary Computer (training imagery)
  worldcover      ESA WorldCover 2021 v200 clipped to a tile
  labels          multi-season consensus filter and boundary ignore band
  build_dataset   notebook 1 pipeline (resumable)
  data            local staging, random crops, validation chips, T-dropout
  model           OlmoEarth encoder + temporal pooling + 10 m stem + decoder
  train           notebook 2 loop (resumable, bf16, LLRD)
  infer           whole-tile inference and the backend load_model/predict contract
  evaluate_tiles  notebook 3 benchmark and calibration set
  calibrate       temperature scaling and reliability
  gis_export      COG/QML/CLR/GPKG/CSV writers and the citation note
  bundle          one AOI -> full GIS bundle
  cdse            Copernicus Process API fetch and the PC-vs-CDSE parity check
  release         model card and the release folder for the g4dn box
  selftest        quick checks before spending compute units
"""

__version__ = "0.1.0"
