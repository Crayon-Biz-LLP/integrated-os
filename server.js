import 'dotenv/config'; // Load env vars before other imports
import express from 'express';
import cors from 'cors';
import pulseHandler from './api/pulse.js';

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors());
app.use(express.json());

// Health Check
app.get('/', (req, res) => {
    res.send('Integrated OS API is running on Railway 🚂');
});

// Routes
// We wrap the Vercel-style handler to be compatible with Express if needed,
// but since we are refactoring pulse.js, we can just mount it directly if signature matches.
// However, the current pulse.js exports `default async function handler(req, res)`.
// Express handlers are `(req, res, next)`. The Vercel signature is `(req, res)`.
// So it should be largely compatible, but we need to ensure `res.status().json()` works.
// Express `res` object has these methods, so it should be fine.
app.all('/api/pulse', pulseHandler);

app.listen(PORT, () => {
    console.log(`Server is running on port ${PORT}`);
});
