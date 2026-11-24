# robot.glb — license and origin

`robot.glb` is derived from the Reachy Mini description published by Pollen Robotics in
[pollen-robotics/reachy_mini](https://github.com/pollen-robotics/reachy_mini), `descriptions/reachy_mini/urdf`
(`robot.urdf` and the 41 STL meshes it references; identical in reachy_mini 1.7.3 and 1.10.0), licensed under the
[Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).

Changes made: converted to glTF 2.0 by `scripts/robot_glb.py` (one node per URDF link and joint, the joint axis and
limits kept as node extras, collision meshes and normals dropped), then simplified 50 % and compressed with
`gltfpack 1.3.0 -cc -kn -ke -si 0.5` (meshopt compression, mesh quantization).

Copyright Pollen Robotics. Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file
except in compliance with the License. You may obtain a copy of the License at
<https://www.apache.org/licenses/LICENSE-2.0>. Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the License for the specific language governing permissions and limitations under
the License.
