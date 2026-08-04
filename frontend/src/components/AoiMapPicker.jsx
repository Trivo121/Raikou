import { useEffect, useRef, useState } from 'react';
// maplibre-gl v6 ships named ESM exports only; there is no default export.
// `Map` is aliased because it otherwise shadows the global Map constructor.
import { AttributionControl, LngLatBounds, Map as MapLibreMap, NavigationControl } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { Square, Trash2 } from 'lucide-react';

// CARTO dark-matter: free, token-free, and already the right palette for the
// workspace, so no CSS filter hack over a light basemap. Attribution is
// required, and the style JSON already carries "© CARTO, © OpenStreetMap
// contributors" on its sources -- passing customAttribution as well renders it
// twice, so the control below only sets `compact`.
const BASEMAP_STYLE = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json';

const EMPTY = { type: 'FeatureCollection', features: [] };

function bboxFeature(bbox) {
  if (!bbox) return EMPTY;
  const { west, south, east, north } = bbox;
  if (![west, south, east, north].every(Number.isFinite)) return EMPTY;
  return {
    type: 'FeatureCollection',
    features: [{
      type: 'Feature',
      properties: {},
      geometry: {
        type: 'Polygon',
        coordinates: [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
      },
    }],
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
  const armedRef = useRef(false);
  // Handlers are attached once to the map; reading them from a ref keeps the
  // map from being torn down and rebuilt whenever a parent re-renders.
  const callbacksRef = useRef({ onChange, onSelect });
  callbacksRef.current = { onChange, onSelect };
  const valueRef = useRef(value);
  valueRef.current = value;

  const [ready, setReady] = useState(false);
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (mapRef.current || !container) return undefined;
    const map = new MapLibreMap({
      container,
      style: BASEMAP_STYLE,
      center: [80.5, 13.5],
      zoom: 4,
      attributionControl: false,
    });
    mapRef.current = map;
    map.addControl(new AttributionControl({ compact: true }));
    map.addControl(new NavigationControl({ showCompass: false }), 'top-right');

    map.on('load', () => {
      map.addSource('footprints', { type: 'geojson', data: EMPTY });
      map.addLayer({
        id: 'footprints-fill',
        type: 'fill',
        source: 'footprints',
        paint: {
          'fill-color': ['case', ['==', ['get', 'selected'], 1], '#38bdf8', '#a1a1aa'],
          'fill-opacity': ['case', ['==', ['get', 'selected'], 1], 0.22, 0.06],
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
            ['==', ['get', 'online'], 0], '#52525b',
            '#a1a1aa',
          ],
          'line-width': ['case', ['==', ['get', 'selected'], 1], 2, 1],
        },
      });

      // The AOI sits above the results so its edge stays readable over a
      // stack of overlapping frames.
      map.addSource('aoi', { type: 'geojson', data: EMPTY });
      map.addLayer({
        id: 'aoi-fill',
        type: 'fill',
        source: 'aoi',
        paint: { 'fill-color': '#38bdf8', 'fill-opacity': 0.12 },
      });
      map.addLayer({
        id: 'aoi-line',
        type: 'line',
        source: 'aoi',
        paint: { 'line-color': '#38bdf8', 'line-width': 2 },
      });

      map.on('click', 'footprints-fill', (event) => {
        if (armedRef.current) return;
        const productId = event.features?.[0]?.properties?.productId;
        if (productId) callbacksRef.current.onSelect?.(String(productId));
      });
      map.on('mouseenter', 'footprints-fill', () => {
        if (!armedRef.current) map.getCanvas().style.cursor = 'pointer';
      });
      map.on('mouseleave', 'footprints-fill', () => {
        if (!armedRef.current) map.getCanvas().style.cursor = '';
      });

      setReady(true);
    });

    const setAoi = (data) => {
      const source = map.getSource('aoi');
      if (source) source.setData(data);
    };

    const handleDown = (event) => {
      if (!armedRef.current) return;
      drawStartRef.current = event.lngLat;
      // Otherwise the drag pans the map out from under the box being drawn.
      map.dragPan.disable();
    };
    const handleMove = (event) => {
      if (!drawStartRef.current) return;
      setAoi(bboxFeature(normalizeBox(drawStartRef.current, event.lngLat)));
    };
    const handleUp = (event) => {
      const start = drawStartRef.current;
      if (!start) return;
      drawStartRef.current = null;
      map.dragPan.enable();
      armedRef.current = false;
      setArmed(false);
      map.getCanvas().style.cursor = '';
      const box = normalizeBox(start, event.lngLat);
      // A click rather than a drag: treat it as a cancelled draw instead of
      // emitting a zero-area box the server would reject.
      if (Math.abs(box.east - box.west) < 1e-4 || Math.abs(box.north - box.south) < 1e-4) {
        setAoi(EMPTY);
        callbacksRef.current.onChange?.(null);
        return;
      }
      callbacksRef.current.onChange?.(box);
    };

    // Releasing the button outside the canvas never reaches the map's own
    // mouseup, which would leave dragPan disabled and the map stuck: panning
    // dead with no visible cause. This fires after the canvas handler, so a
    // normal draw has already cleared the ref and this does nothing.
    const handleWindowUp = () => {
      if (!drawStartRef.current) return;
      drawStartRef.current = null;
      map.dragPan.enable();
      armedRef.current = false;
      setArmed(false);
      map.getCanvas().style.cursor = '';
      setAoi(bboxFeature(valueRef.current));
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
    const source = map.getSource('aoi');
    if (source && !drawStartRef.current) source.setData(bboxFeature(value));
  }, [ready, value]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    const source = map.getSource('footprints');
    if (source) source.setData(footprintCollection(footprints, selectedId));
  }, [ready, footprints, selectedId]);

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
    if (map) map.getCanvas().style.cursor = 'crosshair';
  };

  const clearArea = () => {
    const map = mapRef.current;
    armedRef.current = false;
    setArmed(false);
    if (map) {
      map.getCanvas().style.cursor = '';
      const source = map.getSource('aoi');
      if (source) source.setData(EMPTY);
    }
    onChange?.(null);
  };

  return (
    <div className="overflow-hidden rounded-xl border border-white/[0.08] bg-[#0b0b0e]">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.07] px-3 py-2">
        <p className="text-[11px] text-zinc-500">
          {armed
            ? 'Drag on the map to draw your area.'
            : value
              ? 'Area set. Draw again to replace it.'
              : 'Draw an area to search.'}
        </p>
        <div className="flex gap-1.5">
          <button
            type="button"
            onClick={armDrawing}
            disabled={armed}
            className="inline-flex items-center gap-1.5 rounded-md border border-sky-400/25 bg-sky-400/10 px-2.5 py-1.5 text-[11px] font-semibold text-sky-200 transition hover:bg-sky-400/20 disabled:cursor-not-allowed disabled:opacity-45"
          >
            <Square size={12} /> {armed ? 'Drawing...' : 'Draw area'}
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
    </div>
  );
}
