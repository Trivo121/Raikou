import zipfile
import xml.etree.ElementTree as ET
from fastapi import UploadFile, HTTPException
import io

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

import os
import uuid
import tempfile
import rasterio

def _build_vrt(zip_path: str, tiff_paths: list[str]) -> str:
    """Builds a VRT XML string stacking the given tiff paths from within the zip."""
    # Open the first tiff to get dimensions
    first_tiff = f"/vsizip/{zip_path}/{tiff_paths[0]}"
    with rasterio.open(first_tiff) as src:
        w, h = src.width, src.height
        
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

async def process_grd_file(file: UploadFile) -> dict:
    if not file.filename.endswith('.zip'):
         raise HTTPException(status_code=400, detail="Uploaded file must be a .zip SAFE archive.")
    
    content = await file.read()
    
    try:
        zip_ref = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file format.")
    
    if not validate_zip_is_grd(zip_ref):
        raise HTTPException(status_code=400, detail="The uploaded file is not a valid Sentinel-1 GRD product.")
        
    metadata = extract_metadata(zip_ref)
    
    # Save the zip to a temporary directory
    session_id = uuid.uuid4().hex
    temp_dir = tempfile.gettempdir()
    session_dir = os.path.join(temp_dir, f"raikou_session_{session_id}")
    os.makedirs(session_dir, exist_ok=True)
    
    zip_path = os.path.join(session_dir, "scene.zip")
    with open(zip_path, "wb") as f:
        f.write(content)
        
    # Find measurement tiffs (VV, VH, etc)
    tiffs = [f.filename for f in zip_ref.filelist if '/measurement/' in f.filename and f.filename.endswith('.tiff')]
    # Sort so VV is first if it exists, for consistency
    tiffs.sort(key=lambda x: 0 if 'vv' in x.lower() else (1 if 'vh' in x.lower() else 2))
    
    if not tiffs:
        raise HTTPException(status_code=400, detail="No measurement TIFF files found in the archive.")
        
    # Build VRT
    vrt_xml = _build_vrt(zip_path.replace("\\", "/"), tiffs)
    vrt_path = os.path.join(session_dir, "stacked.vrt")
    with open(vrt_path, "w") as f:
        f.write(vrt_xml)
    
    return {
        "session_id": session_id,
        "vrt_path": vrt_path.replace("\\", "/"),
        "metadata": metadata
    }
