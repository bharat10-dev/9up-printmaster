from flask import Flask, request, send_file, render_template
import os
from werkzeug.utils import secure_filename
from pdf2image import convert_from_path
from PIL import Image, ImageDraw

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return "Koi file nahi mili"
    
    file = request.files['file']
    if file.filename == '':
        return "File select nahi ki"
    
    input_path = os.path.join(UPLOAD_FOLDER, secure_filename(file.filename))
    output_path = os.path.join(UPLOAD_FOLDER, "9up_duplex_" + file.filename)
    
    file.save(input_path)
    
    try:
        create_9up_odd_even_pdf(input_path, output_path)
        return send_file(output_path, as_attachment=True, 
                        download_name="9up_print_ready.pdf")
    except Exception as e:
        return f"Error: {str(e)}"

def create_9up_odd_even_pdf(input_path, output_path):
    poppler_path = os.environ.get('POPPLER_PATH', '/usr/bin')
    
    images = convert_from_path(input_path, dpi=150, poppler_path=poppler_path)
    
    # A4 Size at 150 DPI
    a4_width = 2480
    a4_height = 3508
    
    # Margins in cm → pixels (at 150 DPI)
    margin_cm = 0.2
    border_cm = 0.3
    margin_px = int(margin_cm * 150 / 2.54)   # ≈ 11.8 px
    border_px = int(border_cm * 150 / 2.54)   # ≈ 17.7 px
    
    cell_w = (a4_width - 2 * margin_px) // 3
    cell_h = (a4_height - 2 * margin_px) // 3
    
    new_pages = []
    odd = images[0::2]
    even = images[1::2]
    
    max_b = max(len(odd), len(even))
    
    for i in range(0, max_b, 9):
        # === FRONT SIDE (Odd Pages) ===
        sheet = Image.new('RGB', (a4_width, a4_height), color='white')
        draw = ImageDraw.Draw(sheet)
        
        batch = odd[i:i+9]
        for idx, p in enumerate(batch):
            row = idx // 3
            col = idx % 3
            
            # Calculate position with margin
            x = margin_px + col * cell_w
            y = margin_px + row * cell_h
            
            # Resize image
            resized = p.resize((cell_w - 2*border_px, cell_h - 2*border_px), 
                             Image.Resampling.LANCZOS)
            
            # Paste image with inner border space
            paste_x = x + border_px
            paste_y = y + border_px
            sheet.paste(resized, (paste_x, paste_y))
            
            # Draw border around image
            draw.rectangle(
                [x, y, x + cell_w - 1, y + cell_h - 1],
                outline='black',
                width=3
            )
        
        new_pages.append(sheet)
        
        # === BACK SIDE (Even Pages) ===
        sheet = Image.new('RGB', (a4_width, a4_height), color='white')
        draw = ImageDraw.Draw(sheet)
        
        batch = even[i:i+9]
        for idx, p in enumerate(batch):
            row = idx // 3
            col = idx % 3
            
            x = margin_px + col * cell_w
            y = margin_px + row * cell_h
            
            resized = p.resize((cell_w - 2*border_px, cell_h - 2*border_px), 
                             Image.Resampling.LANCZOS)
            
            paste_x = x + border_px
            paste_y = y + border_px
            sheet.paste(resized, (paste_x, paste_y))
            
            draw.rectangle(
                [x, y, x + cell_w - 1, y + cell_h - 1],
                outline='black',
                width=3
            )
        
        new_pages.append(sheet)
    
    # Save PDF
    new_pages[0].save(output_path, save_all=True, 
                     append_images=new_pages[1:], 
                     resolution=150.0)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)