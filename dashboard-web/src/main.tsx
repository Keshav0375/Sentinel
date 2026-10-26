import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import './styles.css';
import App from './App';
import { createDefaultClient } from './data/client';

const root = document.getElementById('root');
if (!root) throw new Error('#root is missing from index.html');

void createDefaultClient().then((client) => {
  createRoot(root).render(
    <StrictMode>
      <App client={client} />
    </StrictMode>,
  );
});
