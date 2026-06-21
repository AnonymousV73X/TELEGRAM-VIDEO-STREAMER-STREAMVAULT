from PIL import Image
import os

input_path = r'icons\movie.png'
output_path_png = r'icons\movie_nobg_fallback.png'
output_path_ico = r'icons\movie.ico'

img = Image.open(input_path)
img = img.convert("RGBA")
datas = img.getdata()

newData = []
# Assuming the background is white or close to white
for item in datas:
    # change all white (also shades of whites)
    # pixels to transparent
    if item[0] > 230 and item[1] > 230 and item[2] > 230:
        newData.append((255, 255, 255, 0))
    else:
        newData.append(item)

img.putdata(newData)
img.save(output_path_png, "PNG")

# Convert to ICO
img.save(output_path_ico, format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (32, 32)])
print("Done with fallback.")
