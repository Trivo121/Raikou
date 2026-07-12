import zipfile
import xml.etree.ElementTree as ET
# pyrefly: ignore [missing-import]
from fastapi import UploadFile, HTTPException
import io
import os
import uuid
import tempfile
import rasterio
import json
from typing import List

def validate_zip_is_grd(zip_ref: zipfile.ZipFile) -> bool:
    manifest_info = None
    for f in zip_ref.filelist:
        if f.filename.endswith('manifest.safe'):
            manifest_info = f
            break
            
    if not manifest_info:
        return False
        
    with zip_ref.open(manifest_info) as manifest_file:
        content = manifest_file.read().decode('utf-8')
        if "productType>GRD" in content or ("GRD" in content and "SLC" not in content):
             return True
    return False

def extract_metadata(zip_ref: zipfile.ZipFile) -> dict:
    metadata = {
        "polarization": [],
        "acquisition_date": None,
        "bounding_box": None,
        "sensor": None,
        "orbit_direction": None,
        "incidence_angle": None
    }
    
    manifest_info = next((f for f in zip_ref.filelist if f.filename.endswith('manifest.safe')), None)
    if manifest_info:
        with zip_ref.open(manifest_info) as f:
            tree = ET.parse(f)
            root = tree.getroot()
            
            def find_text(tag_suffix):
                for elem in root.iter():
                    if elem.tag.endswith(tag_suffix):
                        return elem.text
                return None

            sensor = find_text('familyName')
            sensor_number = find_text('number')
            if sensor:
                 metadata['sensor'] = sensor + (sensor_number if sensor_number else "")

            metadata['orbit_direction'] = find_text('pass')
            metadata['acquisition_date'] = find_text('startTime')
            
            coords = find_text('coordinates')
            if coords:
                metadata['bounding_box'] = coords

            for elem in root.iter():
                if elem.tag.endswith('transmitterReceiverPolarisation'):
                    if elem.text not in metadata['polarization']:
                        metadata['polarization'].append(elem.text)
                        
    # Find annotation file for incidence angle
    anno_files = [f for f in zip_ref.filelist if '/annotation/' in f.filename and f.filename.endswith('.xml')]
    if anno_files:
        with zip_ref.open(anno_files[0]) as f:
            anno_tree = ET.parse(f)
            anno_root = anno_tree.getroot()
            
            angles = []
            for elem in anno_root.iter():
                if elem.tag == 'incidenceAngle' and elem.text:
                    try:
                        angles.append(float(elem.text))
                    except ValueError:
                        pass
            if angles:
                metadata['incidence_angle'] = round(sum(angles) / len(angles), 2)
                
    return metadata

def _build_vrt(zip_path: str, tiff_paths: list[str]) -> str:
    """Builds a VRT XML string stacking the given tiff paths from within the zip."""
    first_tiff = f"/vsizip/{zip_path}/{tiff_paths[0]}"
    try:
        with rasterio.open(first_tiff) as src:
            w, h = src.width, src.height
    except Exception:
        raise HTTPException(status_code=400, detail=f"Corrupted or invalid TIFF file in archive: {tiff_paths[0]}")
        
    vrt_xml = f'<VRTDataset rasterXSize="{w}" rasterYSize="{h}">\n'
    for i, tiff in enumerate(tiff_paths, 1):
        vsi_path = f"/vsizip/{zip_path}/{tiff}"
        vrt_xml += f'''  <VRTRasterBand dataType="UInt16" band="{i}">
    <SimpleSource>
      <SourceFilename relativeToVRT="0">{vsi_path}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{w}" ySize="{h}" />
      <DstRect xOff="0" yOff="0" xSize="{w}" ySize="{h}" />
    </SimpleSource>
  </VRTRasterBand>\n'''
    vrt_xml += '</VRTDataset>'
    return vrt_xml

def _build_vrt_local(tiff_paths: list[str]) -> str:
    """Builds a VRT XML string stacking local tiff files directly from disk."""
    try:
        with rasterio.open(tiff_paths[0]) as src:
            w, h = src.width, src.height
            # Use source data type
            dt = src.dtypes[0]
            # Map rasterio dtype to VRT datatype
            dtype_map = {
                'uint8': 'Byte',
                'uint16': 'UInt16',
                'int16': 'Int16',
                'uint32': 'UInt32',
                'int32': 'Int32',
                'float32': 'Float32',
                'float64': 'Float64'
            }
            vrt_dt = dtype_map.get(dt, 'Float32')
    except Exception:
        raise HTTPException(status_code=400, detail=f"Corrupted or invalid TIFF file: {os.path.basename(tiff_paths[0])}")
        
    vrt_xml = f'<VRTDataset rasterXSize="{w}" rasterYSize="{h}">\n'
    for i, tiff in enumerate(tiff_paths, 1):
        vrt_xml += f'''  <VRTRasterBand dataType="{vrt_dt}" band="{i}">
    <SimpleSource>
      <SourceFilename relativeToVRT="1">{os.path.basename(tiff)}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{w}" ySize="{h}" />
      <DstRect xOff="0" yOff="0" xSize="{w}" ySize="{h}" />
    </SimpleSource>
  </VRTRasterBand>\n'''
    vrt_xml += '</VRTDataset>'
    return vrt_xml


async def process_zip_upload(file: UploadFile, session_dir: str, session_id: str) -> dict:
    zip_path = os.path.join(session_dir, "scene.zip")
    
    with open(zip_path, "wb") as f:
        while chunk := await file.read(1024 * 1024 * 10):
            f.write(chunk)
            
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            if validate_zip_is_grd(zip_ref):
                metadata = extract_metadata(zip_ref)
                tiffs = [f.filename for f in zip_ref.filelist if '/measurement/' in f.filename and f.filename.endswith('.tiff')]
            else:
                tiffs = [f.filename for f in zip_ref.filelist if f.filename.lower().endswith(('.tif', '.tiff'))]
                metadata = {
                    "polarization": ["Unknown"],
                    "sensor": "Generic Zipped TIFF",
                    "acquisition_date": "Unknown",
                    "bounding_box": "Unknown"
                }
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file format.")
        
    if not tiffs:
        raise HTTPException(status_code=400, detail="No measurement TIFF files found in the archive.")
        
    tiffs.sort(key=lambda x: 0 if 'vv' in x.lower() else (1 if 'vh' in x.lower() else 2))
        
    vrt_xml = _build_vrt(zip_path.replace("\\", "/"), tiffs)
    vrt_path = os.path.join(session_dir, "stacked.vrt")
    with open(vrt_path, "w") as f:
        f.write(vrt_xml)
    
    return {
        "session_id": session_id,
        "vrt_path": vrt_path.replace("\\", "/"),
        "metadata": metadata
    }


async def process_tiff_uploads(files: List[UploadFile], session_dir: str, session_id: str) -> dict:
    tiff_files = []
    json_file = None
    
    for f in files:
        if f.filename.lower().endswith(('.tif', '.tiff')):
            tiff_files.append(f)
        elif f.filename.lower().endswith('.json'):
            json_file = f
            
    if not tiff_files:
        raise HTTPException(status_code=400, detail="No valid TIFF files found.")
        
    # Sort files to prioritize VV then VH if named explicitly
    tiff_files.sort(key=lambda x: 0 if 'vv' in x.filename.lower() else (1 if 'vh' in x.filename.lower() else 2))
    
    local_tiff_paths = []
    for f in tiff_files:
        path = os.path.join(session_dir, os.path.basename(f.filename))
        with open(path, "wb") as out:
            while chunk := await f.read(1024 * 1024 * 10):
                out.write(chunk)
        local_tiff_paths.append(path)
        
    metadata = {
        "polarization": ["Unknown"],
        "sensor": "Generic TIFF",
        "acquisition_date": "Unknown",
        "bounding_box": "Unknown"
    }
    
    if json_file:
        try:
            content = await json_file.read()
            custom_meta = json.loads(content)
            if "properties" in custom_meta:
                props = custom_meta["properties"]
                metadata["sensor"] = props.get("platform", metadata["sensor"])
                metadata["acquisition_date"] = props.get("datetime", metadata["acquisition_date"])
                if "sar:polarizations" in props:
                    metadata["polarization"] = props["sar:polarizations"]
            else:
                metadata["sensor"] = custom_meta.get("sensor", metadata["sensor"])
                metadata["polarization"] = custom_meta.get("polarization", metadata["polarization"])
        except Exception:
            pass

    vrt_xml = _build_vrt_local(local_tiff_paths)
    vrt_path = os.path.join(session_dir, "stacked.vrt")
    with open(vrt_path, "w") as f:
        f.write(vrt_xml)
        
    return {
        "session_id": session_id,
        "vrt_path": vrt_path.replace("\\", "/"),
        "metadata": metadata
    }

async def process_uploaded_files(files: List[UploadFile]) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")
        
    session_id = uuid.uuid4().hex
    temp_dir = tempfile.gettempdir()
    session_dir = os.path.join(temp_dir, f"raikou_session_{session_id}")
    os.makedirs(session_dir, exist_ok=True)
    
    zip_files = [f for f in files if f.filename.lower().endswith('.zip')]
    if zip_files:
        result = await process_zip_upload(zip_files[0], session_dir, session_id)
    else:
        result = await process_tiff_uploads(files, session_dir, session_id)
        
    metadata_path = os.path.join(session_dir, "metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(result["metadata"], f)
        
    return result
