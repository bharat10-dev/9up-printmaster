from flask import Flask, request, send_file, render_template
import os
from werkzeug.utils import secure_filename
from pdf2image import convert_from_path
from PIL import Image

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
    
    # DPI 200 rakho
    images = convert_from_path(input_path, dpi=200, poppler_path=poppler_path)
    
    a4_width = 2480
    a4_height = 3508
    cell_w = a4_width // 3
    cell_h = a4_height // 3
    
    new_pages = []
    odd = images[0::2]
    even = images[1::2]
    
    max_b = max(len(odd), len(even))
    
    for i in range(0, max_b, 9):
        # Front - Odd
        sheet = Image.new('RGB', (a4_width, a4_height), 'white')
        batch = odd[i:i+9]
        for idx, p in enumerate(batch):
            r = idx // 3
            c = idx % 3
            resized = p.resize((cell_w, cell_h), Image.Resampling.LANCZOS)
            sheet.paste(resized, (c*cell_w, r*cell_h))
        new_pages.append(sheet)
        
        # Back - Even
        sheet = Image.new('RGB', (a4_width, a4_height), 'white')
        batch = even[i:i+9]
        for idx, p in enumerate(batch):
            r = idx // 3
            c = idx % 3
            resized = p.resize((cell_w, cell_h), Image.Resampling.LANCZOS)
            sheet.paste(resized, (c*cell_w, r*cell_h))
        new_pages.append(sheet)
    
    # Resolution bhi 200 rakho
    new_pages[0].save(output_path, save_all=True, 
                     append_images=new_pages[1:], 
                     resolution=200.0)
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)