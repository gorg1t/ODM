// Configuration - Replace with your GitHub repository
const GITHUB_REPO = 'gorg1t/ODM'; // Your actual repository

// Platform detection helpers
const platformIcons = {
    windows: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
        <rect x="4" y="2" width="16" height="20" rx="2"/>
        <line x1="8" y1="6" x2="16" y2="6"/>
        <line x1="8" y1="10" x2="16" y2="10"/>
        <line x1="8" y1="14" x2="12" y2="14"/>
    </svg>`,
    linux: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
        <circle cx="12" cy="12" r="10"/>
        <path d="M12 16v-4M12 8h.01"/>
    </svg>`,
    macos: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
        <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/>
        <polyline points="22,6 12,13 2,6"/>
    </svg>`
};

// Fetch and display releases from GitHub
async function loadReleases() {
    const loadingEl = document.getElementById('releases-loading');
    const errorEl = document.getElementById('releases-error');
    const containerEl = document.getElementById('releases-container');

    try {
        const response = await fetch(`https://api.github.com/repos/${GITHUB_REPO}/releases/latest`);

        if (!response.ok) {
            throw new Error('Failed to fetch releases');
        }

        const release = await response.json();

        // Group assets by platform with more specific detection
        const platforms = {
            'windows-amd64': release.assets.find(a =>
                (a.name.toLowerCase().includes('windows') && a.name.toLowerCase().includes('amd64')) ||
                (a.name.toLowerCase().includes('windows') && a.name.toLowerCase().includes('x64'))
            ),
            'linux-ubuntu-amd64': release.assets.find(a =>
                a.name.toLowerCase().includes('ubuntu') && a.name.toLowerCase().includes('amd64')
            ),
            'linux-arch-amd64': release.assets.find(a =>
                a.name.toLowerCase().includes('arch') && a.name.toLowerCase().includes('amd64')
            ),
            'macos-arm64': release.assets.find(a =>
                (a.name.toLowerCase().includes('macos') || a.name.toLowerCase().includes('darwin')) &&
                (a.name.toLowerCase().includes('arm64') || a.name.toLowerCase().includes('apple'))
            ),
            'macos-amd64': release.assets.find(a =>
                (a.name.toLowerCase().includes('macos') || a.name.toLowerCase().includes('darwin')) &&
                a.name.toLowerCase().includes('amd64')
            )
        };

        // Fallback: if specific variants not found, try generic
        if (!platforms['windows-amd64']) {
            platforms['windows-amd64'] = release.assets.find(a =>
                a.name.toLowerCase().includes('windows') || a.name.endsWith('.zip')
            );
        }
        if (!platforms['linux-ubuntu-amd64'] && !platforms['linux-arch-amd64']) {
            const linuxAsset = release.assets.find(a =>
                a.name.toLowerCase().includes('linux') || a.name.endsWith('.tar.gz')
            );
            platforms['linux-ubuntu-amd64'] = linuxAsset;
        }
        if (!platforms['macos-arm64'] && !platforms['macos-amd64']) {
            const macAsset = release.assets.find(a =>
                a.name.toLowerCase().includes('macos') || a.name.toLowerCase().includes('darwin') || a.name.endsWith('.dmg')
            );
            platforms['macos-arm64'] = macAsset;
        }

        // Create download cards
        containerEl.innerHTML = '';

        const platformConfigs = [
            { key: 'windows-amd64', title: 'Windows', desc: 'Windows 10/11 (AMD64)', icon: 'windows' },
            { key: 'linux-ubuntu-amd64', title: 'Linux Ubuntu', desc: 'Ubuntu 20.04+ (AMD64)', icon: 'linux' },
            { key: 'linux-arch-amd64', title: 'Linux Arch', desc: 'Arch Linux (AMD64)', icon: 'linux' },
            { key: 'macos-arm64', title: 'macOS', desc: 'macOS 11+ (Apple Silicon M1/M2)', icon: 'macos' },
            { key: 'macos-amd64', title: 'macOS', desc: 'macOS 11+ (Intel)', icon: 'macos' }
        ];

        platformConfigs.forEach(config => {
            if (platforms[config.key]) {
                containerEl.appendChild(createDownloadCard(
                    config.title,
                    config.desc,
                    platforms[config.key],
                    config.icon
                ));
            }
        });

        // Show container, hide loading
        loadingEl.style.display = 'none';
        containerEl.style.display = 'grid';

    } catch (error) {
        console.error('Error loading releases:', error);
        loadingEl.style.display = 'none';
        errorEl.style.display = 'block';

        // Update GitHub link in error message
        const githubLink = errorEl.querySelector('a');
        if (githubLink) {
            githubLink.href = `https://github.com/${GITHUB_REPO}/releases`;
        }
    }
}

function createDownloadCard(title, description, asset, platform) {
    const card = document.createElement('div');
    card.className = 'download-card';

    const sizeInMB = (asset.size / (1024 * 1024)).toFixed(0);

    card.innerHTML = `
        <div class="download-icon">
            ${platformIcons[platform]}
        </div>
        <h3>${title}</h3>
        <p>${description}</p>
        <a href="${asset.browser_download_url}" class="btn btn-download" download>
            Скачать ${getFileExtension(asset.name)}
        </a>
        <span class="download-size">~${sizeInMB} МБ</span>
        <div class="download-meta">
            <small>Версия: ${asset.name}</small>
        </div>
    `;

    return card;
}

function getFileExtension(filename) {
    if (filename.endsWith('.zip')) return '.zip';
    if (filename.endsWith('.tar.gz')) return '.tar.gz';
    if (filename.endsWith('.dmg')) return '.dmg';
    return '';
}

// Load releases when page loads
document.addEventListener('DOMContentLoaded', loadReleases);

// Smooth scrolling for navigation links
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function (e) {
        e.preventDefault();
        const target = document.querySelector(this.getAttribute('href'));
        if (target) {
            target.scrollIntoView({
                behavior: 'smooth',
                block: 'start'
            });
        }
    });
});

// Navbar background on scroll
const navbar = document.querySelector('.navbar');
let lastScroll = 0;

window.addEventListener('scroll', () => {
    const currentScroll = window.pageYOffset;

    if (currentScroll <= 0) {
        navbar.style.background = 'rgba(15, 23, 42, 0.95)';
    } else {
        navbar.style.background = 'rgba(15, 23, 42, 0.98)';
    }

    lastScroll = currentScroll;
});

// Intersection Observer for fade-in animations
const observerOptions = {
    threshold: 0.1,
    rootMargin: '0px 0px -100px 0px'
};

const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting) {
            entry.target.style.opacity = '1';
            entry.target.style.transform = 'translateY(0)';
        }
    });
}, observerOptions);

// Observe all cards and cases
document.querySelectorAll('.feature-card, .audience-card, .case, .spec-card, .download-card').forEach(el => {
    el.style.opacity = '0';
    el.style.transform = 'translateY(30px)';
    el.style.transition = 'opacity 0.6s ease-out, transform 0.6s ease-out';
    observer.observe(el);
});

// PTZ Interactive Demo
let cameraX = 0;
let cameraY = 0;
let cameraZoom = 1.0;
let ptzInterval = null;

function initPTZDemo() {
    const cameraContent = document.getElementById('camera-content');
    const zoomLevelEl = document.getElementById('zoom-level');
    const ptzButtons = document.querySelectorAll('.ptz-btn[data-action]');

    if (!cameraContent || !zoomLevelEl) return;

    function updateCamera() {
        // Clamp values
        cameraX = Math.max(-100, Math.min(100, cameraX));
        cameraY = Math.max(-80, Math.min(80, cameraY));
        cameraZoom = Math.max(0.5, Math.min(3.0, cameraZoom));

        // Apply transform
        cameraContent.setAttribute('transform',
            `translate(${200 - cameraX}, ${150 - cameraY}) scale(${cameraZoom})`
        );

        // Update zoom indicator
        zoomLevelEl.textContent = cameraZoom.toFixed(1) + 'x';
    }

    function startPTZAction(action) {
        stopPTZAction(); // Stop any existing action

        const speed = 2;

        ptzInterval = setInterval(() => {
            switch(action) {
                case 'up':
                    cameraY -= speed;
                    break;
                case 'down':
                    cameraY += speed;
                    break;
                case 'left':
                    cameraX -= speed;
                    break;
                case 'right':
                    cameraX += speed;
                    break;
                case 'up-left':
                    cameraY -= speed;
                    cameraX -= speed;
                    break;
                case 'up-right':
                    cameraY -= speed;
                    cameraX += speed;
                    break;
                case 'down-left':
                    cameraY += speed;
                    cameraX -= speed;
                    break;
                case 'down-right':
                    cameraY += speed;
                    cameraX += speed;
                    break;
                case 'zoom-in':
                    cameraZoom += 0.05;
                    break;
                case 'zoom-out':
                    cameraZoom -= 0.05;
                    break;
                case 'stop':
                    stopPTZAction();
                    return;
            }
            updateCamera();
        }, 50);
    }

    function stopPTZAction() {
        if (ptzInterval) {
            clearInterval(ptzInterval);
            ptzInterval = null;
        }
    }

    // Attach event listeners to PTZ buttons
    ptzButtons.forEach(btn => {
        const action = btn.getAttribute('data-action');

        btn.addEventListener('mousedown', () => {
            startPTZAction(action);
        });

        btn.addEventListener('mouseup', stopPTZAction);
        btn.addEventListener('mouseleave', stopPTZAction);

        // Touch support
        btn.addEventListener('touchstart', (e) => {
            e.preventDefault();
            startPTZAction(action);
        });

        btn.addEventListener('touchend', (e) => {
            e.preventDefault();
            stopPTZAction();
        });
    });

    // Initialize camera position
    updateCamera();
}

// Initialize PTZ demo when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    initPTZDemo();
});

// PTZ button interactions
document.querySelectorAll('.ptz-btn').forEach(btn => {
    btn.addEventListener('mousedown', function() {
        this.style.transform = 'scale(0.95)';
    });

    btn.addEventListener('mouseup', function() {
        this.style.transform = 'scale(1)';
    });

    btn.addEventListener('mouseleave', function() {
        this.style.transform = 'scale(1)';
    });
});

// Matrix cell click simulation
document.querySelectorAll('.matrix-cell').forEach(cell => {
    cell.addEventListener('click', function() {
        document.querySelectorAll('.matrix-cell').forEach(c => c.classList.remove('active'));
        this.classList.add('active');
    });
});

// Download button analytics (will be added dynamically by loadReleases)
document.addEventListener('click', function(e) {
    if (e.target.classList.contains('btn-download')) {
        const platform = e.target.closest('.download-card').querySelector('h3').textContent;
        console.log(`Download initiated: ${platform}`);
    }
});

// Video placeholder animation
const videoPlaceholder = document.querySelector('.video-placeholder svg');
if (videoPlaceholder) {
    setInterval(() => {
        videoPlaceholder.style.transform = 'scale(1.1)';
        videoPlaceholder.style.opacity = '0.7';
        setTimeout(() => {
            videoPlaceholder.style.transform = 'scale(1)';
            videoPlaceholder.style.opacity = '1';
        }, 1000);
    }, 3000);
}

// Stats counter animation
const animateStats = () => {
    const stats = document.querySelectorAll('.stat-value');

    stats.forEach(stat => {
        const text = stat.textContent;
        const hasNumber = text.match(/\d+/);

        if (hasNumber) {
            const target = parseInt(hasNumber[0]);
            const prefix = text.replace(/\d+.*/, '');
            const suffix = text.replace(/.*\d+/, '');
            let current = 0;
            const increment = target / 50;
            const timer = setInterval(() => {
                current += increment;
                if (current >= target) {
                    stat.textContent = prefix + target + suffix;
                    clearInterval(timer);
                } else {
                    stat.textContent = prefix + Math.floor(current) + suffix;
                }
            }, 30);
        }
    });
};

// Trigger stats animation when hero is visible
const heroObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting) {
            animateStats();
            heroObserver.unobserve(entry.target);
        }
    });
}, { threshold: 0.5 });

const heroSection = document.querySelector('.hero');
if (heroSection) {
    heroObserver.observe(heroSection);
}

// Add CSS animations for notifications
const style = document.createElement('style');
style.textContent = `
    @keyframes slideIn {
        from {
            transform: translateX(100%);
            opacity: 0;
        }
        to {
            transform: translateX(0);
            opacity: 1;
        }
    }

    @keyframes slideOut {
        from {
            transform: translateX(0);
            opacity: 1;
        }
        to {
            transform: translateX(100%);
            opacity: 0;
        }
    }
`;
document.head.appendChild(style);

// Mobile menu toggle (for future implementation)
const createMobileMenu = () => {
    const navbar = document.querySelector('.navbar .container');
    const menuButton = document.createElement('button');
    menuButton.className = 'mobile-menu-button';
    menuButton.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" style="width: 24px; height: 24px;">
            <line x1="3" y1="12" x2="21" y2="12"></line>
            <line x1="3" y1="6" x2="21" y2="6"></line>
            <line x1="3" y1="18" x2="21" y2="18"></line>
        </svg>
    `;
    menuButton.style.cssText = `
        display: none;
        background: transparent;
        border: none;
        color: white;
        cursor: pointer;
        padding: 0.5rem;

        @media (max-width: 968px) {
            display: block;
        }
    `;

    const navMenu = document.querySelector('.nav-menu');
    menuButton.addEventListener('click', () => {
        navMenu.style.display = navMenu.style.display === 'flex' ? 'none' : 'flex';
        navMenu.style.flexDirection = 'column';
        navMenu.style.position = 'absolute';
        navMenu.style.top = '100%';
        navMenu.style.left = '0';
        navMenu.style.right = '0';
        navMenu.style.background = 'rgba(15, 23, 42, 0.98)';
        navMenu.style.padding = '1rem';
    });
};

if (window.innerWidth <= 968) {
    createMobileMenu();
}

// Parallax effect for hero
window.addEventListener('scroll', () => {
    const scrolled = window.pageYOffset;
    const heroContent = document.querySelector('.hero-content');
    const heroVisual = document.querySelector('.hero-visual');

    if (heroContent && scrolled < 800) {
        heroContent.style.transform = `translateY(${scrolled * 0.3}px)`;
        heroContent.style.opacity = 1 - scrolled / 800;
    }

    if (heroVisual && scrolled < 800) {
        heroVisual.style.transform = `translateY(${scrolled * 0.2}px)`;
    }
});

console.log('ONVIF PTZ Controller website loaded successfully');
