import os
import sys
from pathlib import Path

os.environ["NLP_MAP_FROM_LAUNCHER"] = "0"      # tests pin the map; production follows the running navigation map
os.environ["MAP_NAME"] = "office_map"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # make `NLP` importable when pytest runs from anywhere
