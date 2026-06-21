from rembg import remove
from PIL import Image
import io
import os

input_path = r'icons\movie.png'
output_path_png = r'icons\movie_nobg.png'
output_path_ico = r'icons\movie.ico'

# Load image
with open(input_path, 'rb') as i:
    input_image = i.read()

# Remove background
print("Removing background...")
output_image = remove(input_image)

# Save as PNG
with open(output_path_png, 'wb') as o:
    o.write(output_image)

# Open the new transparent PNG and save as ICO
print("Converting to ICO...")
img = Image.open(io.BytesIO(output_image))
img.save(output_path_ico, format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (32, 32)])
print("Done!")
