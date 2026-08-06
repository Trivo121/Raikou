import { useEffect, useRef, useState } from 'react';
// maplibre-gl v6 ships named ESM exports only; there is no default export.
// `Map` is aliased because it otherwise shadows the global Map constructor.
import { AttributionControl, LngLatBounds, Map as MapLibreMap, NavigationControl } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { Layers, Square, Trash2 } from 'lucide-react';

// Sentinel-2 cloudless is the default because this tool picks Sentinel-1
// frames: the user is choosing ground, so they should be looking at ground,
// not at road names. It is a cloud-free mosaic built from Copernicus Sentinel-2
// data, CC BY 4.0, no API key -- the same programme the scenes come from.
//
// Every basemap is declared as a raster source in one style, and switching
// toggles layer visibility. `setStyle` would be the obvious alternative and is
// wrong here: it discards custom sources, so the AOI and the footprints would
// have to be rebuilt on every switch.
//
// A raster source needs one request per tile and nothing else. A vector style
// needs a style JSON, the TileJSON it points at, then glyph and sprite atlases,
// and each of those fails silently -- a constructed map, a parsed style, no log
// line, and an empty box.
const BASEMAPS = [
  { id: 's2', label: 'Imagery', layers: ['base-s2', 'base-labels'] },
  { id: 'osm', label: 'Streets', layers: ['base-osm'] },
];

const BASE_STYLE = {
  version: 8,
  sources: {
    s2: {
      type: 'raster',
      tiles: ['https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg'],
      tileSize: 256,
      maxzoom: 16,
      attribution: 'Sentinel-2 cloudless 2020 by <a href="https://eox.at" target="_blank" rel="noreferrer">EOX</a> (CC BY 4.0), modified Copernicus Sentinel data',
    },
    // Coastlines, borders and place names over the imagery. Without it the
    // mosaic is beautiful and impossible to navigate.
    labels: {
      type: 'raster',
      tiles: ['https://tiles.maps.eox.at/wmts/1.0.0/overlay_base_bright_3857/default/g/{z}/{y}/{x}.jpg'],
      tileSize: 256,
      maxzoom: 16,
      attribution: '',
    },
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      maxzoom: 19,
      attribution: '&copy; OpenStreetMap contributors',
    },
  },
  layers: [
    {
      id: 'base-osm',
      type: 'raster',
      source: 'osm',
      layout: { visibility: 'none' },
      paint: { 'raster-brightness-max': 0.62, 'raster-saturation': -0.45 },
    },
    { id: 'base-s2', type: 'raster', source: 's2', layout: { visibility: 'visible' } },
    {
      id: 'base-labels',
      type: 'raster',
      source: 'labels',
      layout: { visibility: 'visible' },
      paint: { 'raster-opacity': 0.9 },
    },
  ],
};

const EMPTY = { type: 'FeatureCollection', features: [] };
const CORNERS = ['sw', 'se', 'ne', 'nw'];

function bboxFeature(bbox, drawing = false) {
  if (!bbox) return EMPTY;
  const { west, south, east, north } = bbox;
  if (![west, south, east, north].every(Number.isFinite)) return EMPTY;
  return {
    type: 'FeatureCollection',
    features: [{
      type: 'Feature',
      // Drives a heavier outline while the box is being dragged out. A 14%
      // fill behind a 2px line is legible once it is sitting still, but not
      // while you are looking for it to appear over a photograph.
      properties: { drawing: drawing ? 1 : 0 },
      geometry: {
        type: 'Polygon',
        coordinates: [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
      },
    }],
  };
}

// One point per corner, tagged so a drag knows which edges to move.
function handleFeatures(bbox) {
  if (!bbox) return EMPTY;
  const { west, south, east, north } = bbox;
  if (![west, south, east, north].every(Number.isFinite)) return EMPTY;
  const at = { sw: [west, south], se: [east, south], ne: [east, north], nw: [west, north] };
  return {
    type: 'FeatureCollection',
    features: CORNERS.map((corner) => ({
      type: 'Feature',
      properties: { corner },
      geometry: { type: 'Point', coordinates: at[corner] },
    })),
  };
}

function footprintCollection(footprints, selectedId) {
  return {
    type: 'FeatureCollection',
    features: (footprints || [])
      .filter((item) => item?.footprint?.type)
      .map((item) => ({
        type: 'Feature',
        geometry: item.footprint,
        properties: {
          productId: item.product_id,
          // maplibre expressions compare numbers far more predictably than
          // booleans across style versions.
          selected: item.product_id === selectedId ? 1 : 0,
          online: item.online ? 1 : 0,
        },
      })),
  };
}

function normalizeBox(a, b) {
  return {
    west: Math.min(a.lng, b.lng),
    east: Math.max(a.lng, b.lng),
    south: Math.min(a.lat, b.lat),
    north: Math.max(a.lat, b.lat),
  };
}

// Dragging a corner past its opposite edge flips the box; re-normalising keeps
// west<east and south<north so the rest of the code never sees an inside-out
// rectangle.
function moveCorner(bbox, corner, lngLat) {
  const next = { ...bbox };
  if (corner === 'sw' || corner === 'nw') next.west = lngLat.lng;
  if (corner === 'se' || corner === 'ne') next.east = lngLat.lng;
  if (corner === 'sw' || corner === 'se') next.south = lngLat.lat;
  if (corner === 'nw' || corner === 'ne') next.north = lngLat.lat;
  return {
    west: Math.min(next.west, next.east),
    east: Math.max(next.west, next.east),
    south: Math.min(next.south, next.north),
    north: Math.max(next.south, next.north),
  };
}

// Ground span, so the readout is in the units the user is actually reasoning
// about. Equirectangular is right to well under a percent at these sizes and
// avoids pulling in a geodesy dependency for a label.
function extentKm(bbox) {
  if (!bbox) return null;
  const R = 6371;
  const rad = Math.PI / 180;
  const midLat = ((bbox.north + bbox.south) / 2) * rad;
  return {
    width: Math.abs((bbox.east - bbox.west) * rad * R * Math.cos(midLat)),
    height: Math.abs((bbox.north - bbox.south) * rad * R),
  };
}

const round = (n) => Math.round(n * 10000) / 10000;

/**
 * The AOI map. Purely presentational: it owns the maplibre instance and
 * reports a bounding box out, and knows nothing about searching or scenes.
 *
 * No React wrapper library. `react-map-gl` would add a dependency and a
 * version coupling for what is one `useEffect` driving an imperative API.
 */
export default function AoiMapPicker({ value, onChange, footprints, selectedId, onSelect }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const drawStartRef = useRef(null);
  const dragCornerRef = useRef(null);
  const armedRef = useRef(false);
  // Handlers are attached once to the map; reading them from a ref keeps the
  // map from being torn down and rebuilt whenever a parent re-renders.
  const callbacksRef = useRef({ onChange, onSelect });
  callbacksRef.current = { onChange, onSelect };
  const valueRef = useRef(value);
  valueRef.current = value;

  const [ready, setReady] = useState(false);
  const [armed, setArmed] = useState(false);
  const [basemap, setBasemap] = useState('s2');
  // Text drafts for the coordinate boxes, so a half-typed "-" or "12." is not
  // rejected mid-keystroke.
  const [draft, setDraft] = useState(null);

  useEffect(() => {
    const container = containerRef.current;
    if (mapRef.current || !container) return undefined;
    const map = new MapLibreMap({
      container,
      style: BASE_STYLE,
      center: [80.5, 13.5],
      zoom: 4,
      attributionControl: false,
    });
    mapRef.current = map;
    map.addControl(new AttributionControl({ compact: true }));
    map.addControl(new NavigationControl({ showCompass: false }), 'top-right');

    const paint = (box, drawing = false) => {
      const aoi = map.getSource('aoi');
      const handles = map.getSource('aoi-handles');
      if (aoi) aoi.setData(bboxFeature(box, drawing));
      // Corner handles would sit under the cursor mid-drag and are meaningless
      // until the box is committed, so they stay hidden while drawing.
      if (handles) handles.setData(drawing ? EMPTY : handleFeatures(box));
    };

    map.on('load', () => {
      // A 250x170 km frame is large enough to survive simplification, but a
      // footprint is a five-point ring with nothing to spare; keep it exact.
      map.addSource('footprints', { type: 'geojson', data: EMPTY, tolerance: 0 });
      map.addLayer({
        id: 'footprints-fill',
        type: 'fill',
        source: 'footprints',
        paint: {
          'fill-color': ['case', ['==', ['get', 'selected'], 1], '#38bdf8', '#d4d4d8'],
          'fill-opacity': ['case', ['==', ['get', 'selected'], 1], 0.26, 0.10],
        },
      });
      map.addLayer({
        id: 'footprints-line',
        type: 'line',
        source: 'footprints',
        paint: {
          'line-color': [
            'case',
            ['==', ['get', 'selected'], 1], '#38bdf8',
            ['==', ['get', 'online'], 0], '#71717a',
            '#e4e4e7',
          ],
          'line-width': ['case', ['==', ['get', 'selected'], 1], 2.5, 1],
        },
      });

      // The AOI sits above the results so its edge stays readable over a
      // stack of overlapping frames.
      // tolerance:0 is load-bearing, not tuning. maplibre tiles GeoJSON and
      // simplifies it with Douglas-Peucker at a default tolerance of 0.375
      // tile units. A small rectangle collapses under that: the feature stays
      // in the source with exact coordinates, the layer stays visible with
      // valid paint, and no geometry survives into the tile -- so the box a
      // user just dragged out is simply never drawn. Verified the source and
      // layers were correct while nothing rendered; this is the gap between.
      map.addSource('aoi', { type: 'geojson', data: EMPTY, tolerance: 0 });
      map.addLayer({
        id: 'aoi-fill',
        type: 'fill',
        source: 'aoi',
        paint: {
          'fill-color': '#38bdf8',
          'fill-opacity': ['case', ['==', ['get', 'drawing'], 1], 0.28, 0.14],
        },
      });
      // A dark casing under a bright line. Satellite imagery is a photograph:
      // any single colour disappears somewhere in it, over snow or sand or
      // cloud. Two contrasting strokes survive every background, which is the
      // same reason road maps draw roads this way.
      map.addLayer({
        id: 'aoi-casing',
        type: 'line',
        source: 'aoi',
        paint: {
          'line-color': '#0b0b0e',
          'line-width': ['case', ['==', ['get', 'drawing'], 1], 7, 5],
          'line-opacity': 0.75,
        },
      });
      map.addLayer({
        id: 'aoi-line',
        type: 'line',
        source: 'aoi',
        paint: {
          'line-color': ['case', ['==', ['get', 'drawing'], 1], '#7dd3fc', '#38bdf8'],
          'line-width': ['case', ['==', ['get', 'drawing'], 1], 3.5, 2],
        },
      });

      // Corner points are the smallest features on the map and would be the
      // first thing simplification discards.
      map.addSource('aoi-handles', { type: 'geojson', data: EMPTY, tolerance: 0 });
      map.addLayer({
        id: 'aoi-handles',
        type: 'circle',
        source: 'aoi-handles',
        paint: {
          'circle-radius': 6,
          'circle-color': '#38bdf8',
          'circle-stroke-width': 2,
          'circle-stroke-color': '#0b0b0e',
        },
      });

      map.on('click', 'footprints-fill', (event) => {
        if (armedRef.current || dragCornerRef.current) return;
        const productId = event.features?.[0]?.properties?.productId;
        if (productId) callbacksRef.current.onSelect?.(String(productId));
      });
      map.on('mouseenter', 'footprints-fill', () => {
        if (!armedRef.current) map.getCanvas().style.cursor = 'pointer';
      });
      map.on('mouseleave', 'footprints-fill', () => {
        if (!armedRef.current) map.getCanvas().style.cursor = '';
      });
      map.on('mouseenter', 'aoi-handles', () => {
        if (!armedRef.current) map.getCanvas().style.cursor = 'move';
      });
      map.on('mouseleave', 'aoi-handles', () => {
        if (!armedRef.current && !dragCornerRef.current) map.getCanvas().style.cursor = '';
      });

      // Grabbing a corner starts a resize. Registered on the layer so it only
      // competes with panning when the pointer is actually on a handle.
      map.on('mousedown', 'aoi-handles', (event) => {
        if (armedRef.current || !valueRef.current) return;
        const corner = event.features?.[0]?.properties?.corner;
        if (!corner) return;
        event.preventDefault();
        dragCornerRef.current = String(corner);
        map.dragPan.disable();
        map.getCanvas().style.cursor = 'move';
      });

      paint(valueRef.current);
      setReady(true);
    });

    const handleDown = (event) => {
      if (!armedRef.current) return;
      // Panning is already disabled by armDrawing. Disabling it here instead
      // was too late: maplibre's pan handler engages on this same mousedown,
      // so the map slid under the cursor while the box was drawn in geographic
      // coordinates -- which reads as the box never appearing.
      event.preventDefault?.();
      drawStartRef.current = event.lngLat;
    };
    const handleMove = (event) => {
      if (dragCornerRef.current && valueRef.current) {
        paint(moveCorner(valueRef.current, dragCornerRef.current, event.lngLat));
        return;
      }
      if (!drawStartRef.current) return;
      paint(normalizeBox(drawStartRef.current, event.lngLat), true);
    };
    const handleUp = (event) => {
      if (dragCornerRef.current) {
        const corner = dragCornerRef.current;
        dragCornerRef.current = null;
        map.dragPan.enable();
        map.boxZoom.enable();
        map.getCanvas().style.cursor = '';
        if (valueRef.current) {
          const box = moveCorner(valueRef.current, corner, event.lngLat);
          paint(box);
          callbacksRef.current.onChange?.(box);
        }
        return;
      }
      const start = drawStartRef.current;
      if (!start) return;
      drawStartRef.current = null;
      map.dragPan.enable();
      map.boxZoom.enable();
      armedRef.current = false;
      setArmed(false);
      map.getCanvas().style.cursor = '';
      const box = normalizeBox(start, event.lngLat);
      // A click rather than a drag: treat it as a cancelled draw instead of
      // emitting a zero-area box the server would reject.
      if (Math.abs(box.east - box.west) < 1e-4 || Math.abs(box.north - box.south) < 1e-4) {
        paint(null);
        callbacksRef.current.onChange?.(null);
        return;
      }
      paint(box);
      callbacksRef.current.onChange?.(box);
    };

    // Releasing the button outside the canvas never reaches the map's own
    // mouseup, which would leave dragPan disabled and the map stuck: panning
    // dead with no visible cause. This fires after the canvas handler, so a
    // normal draw has already cleared the refs and this does nothing.
    const handleWindowUp = () => {
      if (!drawStartRef.current && !dragCornerRef.current) return;
      drawStartRef.current = null;
      dragCornerRef.current = null;
      map.dragPan.enable();
      map.boxZoom.enable();
      armedRef.current = false;
      setArmed(false);
      map.getCanvas().style.cursor = '';
      paint(valueRef.current);
    };

    map.on('mousedown', handleDown);
    map.on('mousemove', handleMove);
    map.on('mouseup', handleUp);
    globalThis.addEventListener('mouseup', handleWindowUp);

    // maplibre measures the container once, at construction, and never
    // re-measures on its own. This panel mounts inside a lazy Suspense
    // boundary, so the container is routinely still being laid out at that
    // moment: the canvas then keeps whatever width it saw first. Observed
    // live at 400px inside a 724px box, and a container that is momentarily
    // zero-width yields a zero-width canvas -- a blank map, no error anywhere.
    // ResizeObserver fires once on observe, so this covers the initial size
    // as well as later layout changes.
    const resizeObserver = new ResizeObserver(() => { map.resize(); });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      globalThis.removeEventListener('mouseup', handleWindowUp);
      map.remove();
      mapRef.current = null;
      setReady(false);
    };
    // The map is built once. Live values are read through refs so a parent
    // re-render never tears down and rebuilds the GL context.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    if (drawStartRef.current || dragCornerRef.current) return;
    map.getSource('aoi')?.setData(bboxFeature(value));
    map.getSource('aoi-handles')?.setData(handleFeatures(value));
  }, [ready, value]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    const source = map.getSource('footprints');
    if (source) source.setData(footprintCollection(footprints, selectedId));
  }, [ready, footprints, selectedId]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    BASEMAPS.forEach((option) => {
      option.layers.forEach((layerId) => {
        if (map.getLayer(layerId)) {
          map.setLayoutProperty(layerId, 'visibility', option.id === basemap ? 'visible' : 'none');
        }
      });
    });
  }, [ready, basemap]);

  // Frame the results once they arrive so a user is not left looking at an
  // empty ocean after a successful search.
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !(footprints || []).length) return;
    const bounds = new LngLatBounds();
    let extended = false;
    (footprints || []).forEach((item) => {
      const rings = item?.footprint?.coordinates;
      if (!Array.isArray(rings)) return;
      // Polygon nests as [ring][position]; MultiPolygon as [polygon][ring][position].
      const depth = item.footprint.type === 'MultiPolygon' ? 2 : 1;
      rings.flat(depth).forEach((position) => {
        if (Array.isArray(position) && Number.isFinite(position[0]) && Number.isFinite(position[1])) {
          bounds.extend([position[0], position[1]]);
          extended = true;
        }
      });
    });
    if (extended) map.fitBounds(bounds, { padding: 48, maxZoom: 8, duration: 400 });
  }, [ready, footprints]);

  const armDrawing = () => {
    const map = mapRef.current;
    armedRef.current = true;
    setArmed(true);
    if (map) {
      // Off before the gesture starts, not during it. boxZoom shares the
      // drag gesture and would fight the same mousedown.
      map.dragPan.disable();
      map.boxZoom.disable();
      map.getCanvas().style.cursor = 'crosshair';
    }
  };

  const clearArea = () => {
    const map = mapRef.current;
    armedRef.current = false;
    setArmed(false);
    setDraft(null);
    if (map) {
      // Clearing while armed but before drawing would otherwise leave panning
      // disabled with nothing on screen to explain it -- the same dead-map
      // state the window-level mouseup guard exists to prevent.
      map.dragPan.enable();
      map.boxZoom.enable();
      map.getCanvas().style.cursor = '';
      map.getSource('aoi')?.setData(EMPTY);
      map.getSource('aoi-handles')?.setData(EMPTY);
    }
    onChange?.(null);
  };

  // Zoom to the box so "did I plot the right place?" is answerable without
  // hunting for it.
  const zoomToArea = () => {
    const map = mapRef.current;
    if (!map || !value) return;
    map.fitBounds(
      new LngLatBounds([value.west, value.south], [value.east, value.north]),
      { padding: 56, duration: 400 },
    );
  };

  const commitEdge = (edge, raw) => {
    const parsed = Number.parseFloat(raw);
    if (!Number.isFinite(parsed) || !value) return;
    const limit = edge === 'north' || edge === 'south' ? 90 : 180;
    if (Math.abs(parsed) > limit) return;
    const next = { ...value, [edge]: parsed };
    if (next.west >= next.east || next.south >= next.north) return;
    onChange?.(next);
  };

  const span = extentKm(value);
  const edgeValue = (edge) => (draft && draft.edge === edge ? draft.text : value ? String(round(value[edge])) : '');

  return (
    <div className="overflow-hidden rounded-xl border border-white/[0.08] bg-[#0b0b0e]">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.07] px-3 py-2">
        <p className="text-[11px] text-zinc-500">
          {armed
            ? 'Drag on the map to draw your area.'
            : value
              ? 'Drag a corner to adjust, or type exact edges below.'
              : 'Draw an area to search.'}
        </p>
        <div className="flex flex-wrap items-center gap-1.5">
          <div className="mr-1 inline-flex items-center gap-1 rounded-md border border-white/[0.1] p-0.5" role="group" aria-label="Basemap">
            <Layers size={11} className="ml-1 text-zinc-500" />
            {BASEMAPS.map((option) => (
              <button
                key={option.id}
                type="button"
                onClick={() => setBasemap(option.id)}
                aria-pressed={basemap === option.id}
                className={`rounded px-2 py-1 text-[11px] font-semibold transition ${
                  basemap === option.id ? 'bg-white/[0.12] text-white' : 'text-zinc-400 hover:text-zinc-200'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={armDrawing}
            disabled={armed}
            className="inline-flex items-center gap-1.5 rounded-md border border-sky-400/25 bg-sky-400/10 px-2.5 py-1.5 text-[11px] font-semibold text-sky-200 transition hover:bg-sky-400/20 disabled:cursor-not-allowed disabled:opacity-45"
          >
            <Square size={12} /> {armed ? 'Drawing...' : value ? 'Redraw' : 'Draw area'}
          </button>
          {value && (
            <button
              type="button"
              onClick={clearArea}
              className="inline-flex items-center gap-1.5 rounded-md border border-white/[0.1] px-2.5 py-1.5 text-[11px] font-semibold text-zinc-300 transition hover:bg-white/[0.06]"
            >
              <Trash2 size={12} /> Clear
            </button>
          )}
        </div>
      </div>

      <div ref={containerRef} className="h-[22rem] w-full" role="application" aria-label="Area of interest map" />

      {/* The numeric edges are the answer to "is my box actually where I think
          it is?" -- a drawn rectangle on a photograph is not verifiable by
          eye, and a typed coordinate is. They double as the edit control for
          anyone who has a precise extent already. */}
      {value && (
        <div className="border-t border-white/[0.07] px-3 py-2.5">
          <div className="flex flex-wrap items-end gap-2">
            {[
              ['west', 'W'], ['south', 'S'], ['east', 'E'], ['north', 'N'],
            ].map(([edge, label]) => (
              <label key={edge} className="flex flex-col gap-1">
                <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">{label}</span>
                <input
                  type="number"
                  step="0.01"
                  inputMode="decimal"
                  value={edgeValue(edge)}
                  onChange={(event) => setDraft({ edge, text: event.target.value })}
                  onBlur={(event) => { commitEdge(edge, event.target.value); setDraft(null); }}
                  onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); event.currentTarget.blur(); } }}
                  className="w-[5.5rem] rounded-md border border-white/[0.1] bg-[#09090b] px-2 py-1.5 font-mono text-[11px] text-white outline-none focus:border-sky-400/60 focus:ring-2 focus:ring-sky-400/10"
                  aria-label={`${edge} edge, degrees`}
                />
              </label>
            ))}
            <button
              type="button"
              onClick={zoomToArea}
              className="ml-auto rounded-md border border-white/[0.1] px-2.5 py-1.5 text-[11px] font-semibold text-zinc-300 transition hover:bg-white/[0.06]"
            >
              Zoom to area
            </button>
          </div>
          {span && (
            <p className="mt-2 text-[11px] text-zinc-500">
              Roughly <span className="font-mono text-zinc-300">{span.width.toFixed(0)} x {span.height.toFixed(0)} km</span>
              {' '}&middot; a Sentinel-1 IW frame covers about 250 x 170 km, so this box picks
              which frame to use. That area is then cut out of it at full 10 m resolution,
              up to 25 km a side.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
