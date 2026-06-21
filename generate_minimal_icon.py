from PIL import Image, ImageDraw, ImageFilter

size = 512
scale = size / 512
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
sd = ImageDraw.Draw(shadow)
sd.rounded_rectangle(
    [54, 58, 458, 462],
    radius=118,
    fill=(0, 0, 0, 112),
)
shadow = shadow.filter(ImageFilter.GaussianBlur(18))
img.alpha_composite(shadow)

draw = ImageDraw.Draw(img)
draw.rounded_rectangle(
    [48, 42, 464, 458],
    radius=124,
    fill=(15, 15, 16, 255),
)
draw.rounded_rectangle(
    [72, 66, 440, 434],
    radius=104,
    fill=(245, 197, 24, 255),
)
draw.rounded_rectangle(
    [94, 88, 418, 412],
    radius=88,
    fill=(255, 220, 74, 255),
)

draw.polygon(
    [(214, 168), (214, 344), (350, 256)],
    fill=(14, 14, 15, 255),
)

# Subtle highlight keeps the mark modern without introducing sharp corners.
highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
hd = ImageDraw.Draw(highlight)
hd.rounded_rectangle(
    [96, 88, 416, 242],
    radius=86,
    fill=(255, 255, 255, 28),
)
img.alpha_composite(highlight)

output_png = r"icons\movie.png"
output_ico = r"icons\movie.ico"
img.save(output_png, "PNG")
img.save(
    output_ico,
    format="ICO",
    sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
)

print("Rounded StreamVault icon generated.")
