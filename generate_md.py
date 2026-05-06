import os

root_dir = r"c:\Magang\billboard-tracker"
output_file = r"c:\Magang\billboard-tracker\project_codebase.md"

ignore_dirs = {
    '.git', '__pycache__', 'node_modules', 'build', 'dist', 'out', 'output', 'venv', '.venv', 'resources', 'ref'
}

include_exts = {
    '.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.css', '.json', '.yml', '.yaml', '.ini', '.spec', '.md', '.txt'
}

ignore_files = {
    'package-lock.json', 'yolov8n.pt', 'project_codebase.md'
}

# Max file size to include (1 MB)
MAX_FILE_SIZE = 1 * 1024 * 1024

with open(output_file, 'w', encoding='utf-8') as md:
    md.write("# Project Codebase\n\n")
    for root, dirs, files in os.walk(root_dir):
        # modify dirs in place to skip ignored directories
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        
        for file in files:
            if file in ignore_files or file == "generate_md.py":
                continue
                
            ext = os.path.splitext(file)[1].lower()
            if ext not in include_exts:
                continue
                
            file_path = os.path.join(root, file)
            
            # Skip if file is too large
            if os.path.getsize(file_path) > MAX_FILE_SIZE:
                continue
                
            rel_path = os.path.relpath(file_path, root_dir)
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    
                md.write(f"## `{rel_path}`\n\n")
                md.write(f"```{'python' if ext == '.py' else 'javascript' if ext in ['.js', '.jsx', '.cjs', '.mjs'] else 'typescript' if ext in ['.ts', '.tsx'] else 'html' if ext == '.html' else 'css' if ext == '.css' else 'json' if ext == '.json' else ''}\n")
                md.write(content)
                if not content.endswith('\n'):
                    md.write("\n")
                md.write("```\n\n")
            except Exception as e:
                pass

print(f"Codebase exported to {output_file}")
