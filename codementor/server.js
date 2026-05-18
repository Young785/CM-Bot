const http = require('http');
const fs = require('fs');
const path = require('path');
const { exec } = require('child_process');

const PORT = 8080;

// MIME types
const mimeTypes = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon'
};

const server = http.createServer((req, res) => {
  console.log(`${req.method} ${req.url}`);
  
  // Parse URL
  let filePath = '.' + req.url;
  if (filePath === './') {
    filePath = './test-dashboard.html';
  }
  
  const extname = String(path.extname(filePath)).toLowerCase();
  const contentType = mimeTypes[extname] || 'application/octet-stream';
  
  fs.readFile(filePath, (error, content) => {
    if (error) {
      if (error.code === 'ENOENT') {
        res.writeHead(404, { 'Content-Type': 'text/html' });
        res.end('<h1>404 Not Found</h1>', 'utf-8');
      } else {
        res.writeHead(500);
        res.end('Server Error: ' + error.code + ' ..\n');
      }
    } else {
      res.writeHead(200, { 'Content-Type': contentType });
      res.end(content, 'utf-8');
    }
  });
});

server.listen(PORT, () => {
  console.log(`
╔════════════════════════════════════════════════════════════╗
║     Codementor Bot Test Server                             ║
╠════════════════════════════════════════════════════════════╣
║                                                            ║
║  Dashboard: http://localhost:${PORT}/                    ║
║                                                            ║
║  Saved Pages:                                              ║
║    /index.html    - Open requests list (Account A2 view) ║
║    /details.html  - Request detail page                  ║
║    /msg_user.html - Message/chat interface               ║
║                                                            ║
║  Press Ctrl+C to stop                                      ║
╚════════════════════════════════════════════════════════════╝
  `);
  
  // Auto-open browser
  const url = `http://localhost:${PORT}/`;
  const cmd = process.platform === 'darwin' ? `open ${url}` : 
              process.platform === 'win32' ? `start ${url}` : `xdg-open ${url}`;
  exec(cmd);
});
