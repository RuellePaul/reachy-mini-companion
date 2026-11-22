"""One-shot, by hand: the robot's URDF and the STLs it uses -> frontend/public/robot.glb (then commit it). Not in CI.

The GLB's node tree is the URDF's: one node per link and per joint, named after it, the joint's origin as its
transform and `extras = {type, axis, lower, upper}` (GLTFLoader copies them to `userData`), and one child node per
<visual>. A mesh several visuals use is stored once, positions and indices only: the dashboard computes normals.
From a URDF folder, a part the package's mjcf/assets also holds is read there: the same part in the same frame,
up to 3.3 times finer (urdf/assets is decimated to 6 290 triangles a part, which spikes the face around the
glasses). Then gltfpack 1.3.0: meshopt compression, quantisation, names and extras kept, 30 % of the triangles.

usage: .venv/bin/python scripts/robot_glb.py [SOURCE] [OUT]
  SOURCE: a URDF folder (default: the reachy_mini package's descriptions/reachy_mini/urdf, found without importing
          it) or the daemon's base URL (http://reachy-mini.local:8000: read-only GETs of /api/kinematics/*).
  OUT:    default frontend/public/robot.glb
needs: numpy, and npx (gltfpack@1.3.0 is fetched on first use).
"""

from __future__ import annotations

import importlib.util
import json
import math
import struct
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
STL = np.dtype([('normal', '<f4', 3), ('v', '<f4', (3, 3)), ('attr', '<u2')])
GLTFPACK = ['npx', '--yes', 'gltfpack@1.3.0', '-cc', '-kn', '-ke', '-si', '0.3']


def default_source() -> str:
    spec = importlib.util.find_spec('reachy_mini')
    if spec is None or not spec.submodule_search_locations:
        sys.exit('reachy_mini is not installed here: pass a URDF folder or the daemon URL')
    return str(Path(next(iter(spec.submodule_search_locations))) / 'descriptions' / 'reachy_mini' / 'urdf')


def fetch(source: str, rel: str) -> bytes:
    if not source.startswith('http'):
        finer = Path(source).parent / 'mjcf' / rel
        return (finer if rel.endswith('.stl') and finer.exists() else Path(source) / rel).read_bytes()
    urdf = rel == 'robot.urdf'
    url = f'{source}/api/kinematics/urdf' if urdf else f'{source}/api/kinematics/stl/{Path(rel).name}'
    with urllib.request.urlopen(url, timeout=10) as reply:
        data = reply.read()
    return json.loads(data)['urdf'].encode() if urdf else data


def stl_mesh(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """A binary STL, welded: positions float32 [n, 3], indices uint32 [m]."""
    (count,) = struct.unpack_from('<I', data, 80)
    corners = np.frombuffer(data, STL, count, 84)['v'].reshape(-1, 3)
    positions, indices = np.unique(corners, axis=0, return_inverse=True)
    return positions.astype(np.float32), indices.astype(np.uint32).ravel()


def quaternion(roll: float, pitch: float, yaw: float) -> list[float]:
    """URDF rpy (R = Rz(yaw) Ry(pitch) Rx(roll)) as glTF's [x, y, z, w]."""
    cr, sr, cp, sp, cy, sy = (f(a / 2) for a in (roll, pitch, yaw) for f in (math.cos, math.sin))
    return [
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
        cr * cp * cy + sr * sp * sy,
    ]


def transform(origin: ET.Element | None) -> dict:
    if origin is None:
        return {}
    xyz = [float(v) for v in origin.get('xyz', '0 0 0').split()]
    rpy = [float(v) for v in origin.get('rpy', '0 0 0').split()]
    return {**({'translation': xyz} if any(xyz) else {}), **({'rotation': quaternion(*rpy)} if any(rpy) else {})}


class Gltf:
    """A glTF document filled from the URDF tree, and its binary buffer."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.doc: dict = {'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{'nodes': []}]}
        for key in ('nodes', 'meshes', 'materials', 'accessors', 'bufferViews', 'buffers'):
            self.doc[key] = []
        self.blob = bytearray()
        self.materials: dict[str, int] = {}
        self.meshes: dict[tuple[str, int], int] = {}

    def add(self, key: str, item: dict) -> int:
        self.doc[key].append(item)
        return len(self.doc[key]) - 1

    def view(self, data: bytes, target: int) -> int:
        self.blob.extend(b'\0' * (-len(self.blob) % 4))
        view = {'buffer': 0, 'byteOffset': len(self.blob), 'byteLength': len(data), 'target': target}
        self.blob.extend(data)
        return self.add('bufferViews', view)

    def material(self, rgba: str) -> int:
        if rgba not in self.materials:
            color = [float(v) for v in rgba.split()]
            pbr = {'baseColorFactor': color, 'metallicFactor': 0, 'roughnessFactor': 0.7}
            blend = {'alphaMode': 'BLEND'} if color[3] < 1 else {}
            self.materials[rgba] = self.add('materials', {'pbrMetallicRoughness': pbr, **blend})
        return self.materials[rgba]

    def mesh(self, filename: str, material: int) -> int:
        key = (filename, material)
        if key not in self.meshes:
            positions, indices = stl_mesh(fetch(self.source, filename.removeprefix('package://')))
            bounds = {'min': positions.min(0).tolist(), 'max': positions.max(0).tolist()}
            position = self.add(
                'accessors',
                {'bufferView': self.view(positions.tobytes(), 34962), 'componentType': 5126, 'count': len(positions)}
                | {'type': 'VEC3', **bounds},
            )
            index = self.add(
                'accessors',
                {'bufferView': self.view(indices.tobytes(), 34963), 'componentType': 5125, 'count': len(indices)}
                | {'type': 'SCALAR'},
            )
            primitive = {'attributes': {'POSITION': position}, 'indices': index, 'material': material}
            self.meshes[key] = self.add('meshes', {'name': Path(filename).stem, 'primitives': [primitive]})
        return self.meshes[key]

    def glb(self) -> bytes:
        self.blob.extend(b'\0' * (-len(self.blob) % 4))
        self.doc['buffers'].append({'byteLength': len(self.blob)})
        text = json.dumps(self.doc, separators=(',', ':')).encode()
        text += b' ' * (-len(text) % 4)
        header = struct.pack('<III', 0x46546C67, 2, 28 + len(text) + len(self.blob))
        chunks = struct.pack('<II', len(text), 0x4E4F534A) + text + struct.pack('<II', len(self.blob), 0x004E4942)
        return header + chunks + bytes(self.blob)


def build(source: str) -> bytes:
    urdf = ET.fromstring(fetch(source, 'robot.urdf'))
    links = {link.get('name'): link for link in urdf.findall('link')}
    joints = urdf.findall('joint')
    children: dict[str, list[ET.Element]] = {}
    for joint in joints:
        children.setdefault(joint.find('parent').get('link'), []).append(joint)
    (base,) = set(links) - {joint.find('child').get('link') for joint in joints}
    gltf = Gltf(source)

    def link_node(name: str) -> int:
        kids = []
        for i, visual in enumerate(links[name].findall('visual')):
            mesh, color = visual.find('geometry/mesh'), visual.find('material/color')
            if mesh is not None:
                material = gltf.material(color.get('rgba') if color is not None else '0.5 0.5 0.5 1')
                node = {'name': f'{name}_visual_{i}', 'mesh': gltf.mesh(mesh.get('filename'), material)}
                kids.append(gltf.add('nodes', node | transform(visual.find('origin'))))
        kids += [joint_node(joint) for joint in children.get(name, [])]
        return gltf.add('nodes', {'name': name, **({'children': kids} if kids else {})})

    def joint_node(joint: ET.Element) -> int:
        child = link_node(joint.find('child').get('link'))
        extras: dict = {'type': joint.get('type')}
        if joint.get('type') != 'fixed':
            limit = joint.find('limit')
            extras['axis'] = [float(v) for v in joint.find('axis').get('xyz').split()]
            extras.update(lower=float(limit.get('lower')), upper=float(limit.get('upper')))
        node = {'name': joint.get('name'), 'children': [child], 'extras': extras}
        return gltf.add('nodes', node | transform(joint.find('origin')))

    gltf.doc['scenes'][0]['nodes'].append(link_node(base))
    return gltf.glb()


def main() -> None:
    source = (sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else default_source()).rstrip('/')
    out = Path(sys.argv[2] if len(sys.argv) > 2 else ROOT / 'frontend' / 'public' / 'robot.glb')
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / 'raw.glb'
        raw.write_bytes(build(source))
        subprocess.run([*GLTFPACK, '-i', str(raw), '-o', str(out)], check=True)
    print(out, out.stat().st_size, 'bytes')


if __name__ == '__main__':
    main()
