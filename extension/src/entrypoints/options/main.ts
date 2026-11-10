import '@/styles/page.css';

import { mount } from 'svelte';

import { APP_NAME } from '@/lib/brand';

import Options from './Options.svelte';

document.title = `${APP_NAME} settings`;
mount(Options, { target: document.getElementById('app')! });
