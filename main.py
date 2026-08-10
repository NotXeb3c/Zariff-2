from PySide6 import QtWidgets, QtCore, QtGui
from ollama import Client
import json
import os

class ZariffWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Zariff AI Assistant")
        self.setGeometry(100, 100, 800, 600)
        
        self.central_widget = QtWidgets.QWidget()
        self.setCentralWidget(self.central_widget)
        
        self.create_ui()
        self.setup_connections()
        
        self.ollama_client = Client(host="http://localhost:11434")
        self.conversation_history = []
        
    def create_ui(self):
        layout = QtWidgets.QVBoxLayout()
        
        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.status_label.setStyleSheet("color: #666;")
        
        self.chat_display = QtWidgets.QPlainTextEdit()
        self.chat_display.setReadOnly(True)
        self.chat_display.setStyleSheet("background-color: #1e1e2f; color: #dcdce4; font-family: monospace;")
        
        self.input_layout = QtWidgets.QHBoxLayout()
        self.input_text = QtWidgets.QLineEdit()
        self.input_text.setPlaceholderText("Type your message...")
        self.send_button = QtWidgets.QPushButton("Send")
        self.send_button.setStyleSheet("background-color: #5a67d8; color: white;")
        
        self.input_layout.addWidget(self.input_text)
        self.input_layout.addWidget(self.send_button)
        
        layout.addWidget(self.status_label)
        layout.addWidget(self.chat_display)
        layout.addLayout(self.input_layout)
        
        self.central_widget.setLayout(layout)
        
    def setup_connections(self):
        self.send_button.clicked.connect(self.send_message)
        self.input_text.returnPressed.connect(self.send_message)
        
    def send_message(self):
        message = self.input_text.text().strip()
        if not message:
            return
        
        self.input_text.clear()
        self.status_label.setText("Thinking...")
        self.status_label.setStyleSheet("color: #ff9898;")
        
        self.conversation_history.append({"role": "user", "content": message})
        self.update_chat_display()
        
        try:
            response = self.ollama_client.chat(model="qwen3:8b", messages=self.conversation_history)
            assistant_response = response.get("message", {}).get("content", "")
            
            self.conversation_history.append({"role": "assistant", "content": assistant_response})
            self.update_chat_display()
            self.status_label.setText("Ready")
            self.status_label.setStyleSheet("color: #666;")
        except Exception as e:
            self.status_label.setText(f"Error: {str(e)}")
            self.status_label.setStyleSheet("color: #ff5252;")
            
    def update_chat_display(self):
        self.chat_display.setPlainText("\n".join([f"{msg["role"]}: {msg["content"]}" for msg in self.conversation_history]))

if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    window = ZariffWindow()
    window.show()
    app.exec()